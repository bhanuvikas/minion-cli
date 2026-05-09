"""permissions.py — tiered allow-list for trusting tool calls without prompting.

Three storage tiers (lowest → highest priority, all merged when checking):
  Session  — in-memory only, gone on quit
  Project  — .minion/permissions.toml in cwd, gitignored
  Global   — ~/.minion/permissions.toml

TOML format (written manually, no tomli_w dep):
    [allow]
    run_shell  = ["pytest *", "git status"]
    web_fetch  = ["docs.python.org/*"]
    edit_file  = ["tests/*"]
    write_file = ["tests/*"]

Public API:
    PermissionStore(project_cwd)
        .is_trusted(tool, command) -> bool
        .add_rule(tool, pattern, scope)
    split_compound(command) -> list[str]
    suggest_patterns_for_tool(tool, value) -> list[str]
"""

from __future__ import annotations

import fnmatch
import shlex
import threading
import tomllib
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


_GLOBAL_PERMISSIONS_PATH = Path.home() / ".minion" / "permissions.toml"


# ─── Compound command splitter ─────────────────────────────────────────────────

def split_compound(command: str) -> list[str]:
    """Split a shell command on && and ; only (not |), quote-aware.

    Returns [command] unchanged when no splitting operators are found.
    Empty parts are dropped. Whitespace is stripped from each part.
    """
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
        elif ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
        elif not in_single and not in_double:
            if ch == '&' and i + 1 < n and command[i + 1] == '&':
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                i += 2
            elif ch == ';':
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                i += 1
            else:
                current.append(ch)
                i += 1
        else:
            current.append(ch)
            i += 1

    part = "".join(current).strip()
    if part:
        parts.append(part)

    return parts if parts else [command]


# ─── Pattern suggestion ────────────────────────────────────────────────────────

def _suggest_command_patterns(cmd: str) -> list[str]:
    """Generate up to 5 progressively-relaxed patterns for a shell command.

    Exact pattern is always first; subsequent patterns replace trailing tokens
    with * from right to left.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return [cmd]  # unmatched quotes — exact only

    if not tokens:
        return [cmd]

    patterns: list[str] = [cmd]
    for k in range(1, min(len(tokens), 5)):
        prefix = tokens[:len(tokens) - k]
        patterns.append((" ".join(prefix) + " *") if prefix else "*")
        if len(patterns) >= 5:
            break

    return patterns


def _suggest_url_patterns(url: str) -> list[str]:
    """Generate progressively-relaxed patterns for a URL.

    exact → parent-path wildcard → domain wildcard
    """
    patterns = [url]
    try:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")

        if "/" in path:
            parent = path.rsplit("/", 1)[0]
            patterns.append(f"{base}{parent}/*" if parent else f"{base}/*")
        elif path:
            patterns.append(f"{base}/*")

        domain_wild = f"{base}/*"
        if domain_wild not in patterns:
            patterns.append(domain_wild)
    except Exception:
        pass

    return patterns


def _suggest_path_patterns(path: str) -> list[str]:
    """Generate progressively-relaxed patterns for a file path.

    exact → parent/*.ext (when extension exists) → parent/*
    """
    p = Path(path)
    patterns = [path]
    parent = str(p.parent) if str(p.parent) != "." else ""
    suffix = p.suffix

    if suffix and parent:
        patterns.append(f"{parent}/*{suffix}")
        patterns.append(f"{parent}/*")
    elif suffix:
        patterns.append(f"*{suffix}")
    elif parent:
        patterns.append(f"{parent}/*")

    return patterns


def suggest_patterns_for_tool(tool: str, value: str) -> list[str]:
    """Return progressively-relaxed match patterns for the given tool and value.

    The exact value is always first.
    """
    if tool == "run_shell":
        return _suggest_command_patterns(value)
    elif tool == "web_fetch":
        return _suggest_url_patterns(value)
    elif tool in ("write_file", "edit_file"):
        return _suggest_path_patterns(value)
    else:
        return [value]


# ─── TOML I/O ─────────────────────────────────────────────────────────────────

def _load_toml_permissions(path: Path) -> dict[str, list[str]]:
    """Load [allow] section from a permissions.toml. Returns {} on any error."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        allow = raw.get("allow", {})
        return {k: list(v) for k, v in allow.items() if isinstance(v, list)}
    except (tomllib.TOMLDecodeError, OSError, FileNotFoundError):
        return {}


def _write_toml_allow(path: Path, data: dict[str, list[str]]) -> None:
    """Write [allow] section to a permissions.toml without tomli_w dependency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[allow]"]
    for tool_name, patterns in sorted(data.items()):
        entries = ", ".join(f'"{p}"' for p in patterns)
        lines.append(f"{tool_name} = [{entries}]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ensure_gitignore(minion_dir: Path) -> None:
    """Append 'permissions.toml' to .minion/.gitignore if not already listed."""
    gitignore = minion_dir / ".gitignore"
    entry = "permissions.toml"
    if gitignore.exists():
        text = gitignore.read_text(encoding="utf-8")
        if entry in text.splitlines():
            return
        sep = "" if text.endswith("\n") else "\n"
        gitignore.write_text(text + sep + entry + "\n", encoding="utf-8")
    else:
        gitignore.write_text(entry + "\n", encoding="utf-8")


# ─── PermissionStore ──────────────────────────────────────────────────────────

class PermissionStore:
    """Multi-tier allow-list store: session (memory), project (file), global (file).

    Thread-safe: a single Lock guards all write operations (file + in-memory).

    is_trusted() is fail-safe: returns False when no rules exist for the tool,
    and requires ALL parts of a compound shell command to individually match.
    """

    def __init__(self, project_cwd: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._session: dict[str, list[str]] = {}

        self._project_path: Optional[Path] = (
            Path(project_cwd) / ".minion" / "permissions.toml"
            if project_cwd is not None else None
        )
        self._global_path: Path = _GLOBAL_PERMISSIONS_PATH

        self._project: dict[str, list[str]] = (
            _load_toml_permissions(self._project_path)
            if self._project_path is not None else {}
        )
        self._global: dict[str, list[str]] = _load_toml_permissions(self._global_path)

    def is_trusted(self, tool: str, command: str) -> bool:
        """Return True iff every part of command is covered by a rule for this tool.

        Fail-safe: returns False when no rules exist at all for the tool.
        """
        parts = split_compound(command) if tool == "run_shell" else [command]
        rules = (
            self._session.get(tool, [])
            + self._project.get(tool, [])
            + self._global.get(tool, [])
        )
        if not rules:
            return False
        return all(
            any(fnmatch.fnmatch(part, rule) for rule in rules)
            for part in parts
        )

    def add_rule(self, tool: str, pattern: str, scope: str) -> None:
        """Add a trust rule. scope: 'session' | 'project' | 'global'."""
        with self._lock:
            if scope == "session":
                lst = self._session.setdefault(tool, [])
                if pattern not in lst:
                    lst.append(pattern)

            elif scope == "project":
                lst = self._project.setdefault(tool, [])
                if pattern not in lst:
                    lst.append(pattern)
                if self._project_path is not None:
                    is_new = not self._project_path.exists()
                    _write_toml_allow(self._project_path, self._project)
                    if is_new:
                        _ensure_gitignore(self._project_path.parent)

            elif scope == "global":
                lst = self._global.setdefault(tool, [])
                if pattern not in lst:
                    lst.append(pattern)
                _write_toml_allow(self._global_path, self._global)
