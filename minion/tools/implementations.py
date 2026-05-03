"""Tool implementations — pure Python functions that execute tool calls.

Each function:
  - Takes keyword arguments matching its tool schema in definitions.py
  - Returns a plain string (the observation injected back into LLM context)
  - Never raises — all errors are caught and returned as descriptive strings
    so the model can reason about failures and recover gracefully

No UX or confirmation logic lives here. That's executor.py's responsibility.
"""

import ast
import re
import subprocess
from pathlib import Path
from typing import Optional

from .outline import get_outline
from ..context.filetree import IgnoreRules

DEFAULT_TIMEOUT = 30
_READ_LINE_LIMIT = 300   # lines shown before suggesting a range
_MAX_SEARCH_RESULTS = 50


# ─── read_file ────────────────────────────────────────────────────────────────

def read_file(
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """Read a file, optionally restricted to a line range.

    Always prefixes output with line numbers so the model can reference them
    in subsequent start_line / end_line calls without counting manually.
    """
    try:
        p = Path(path)
        if not p.exists():
            return f"Error: '{path}' does not exist."
        if not p.is_file():
            return f"Error: '{path}' is not a file."

        lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        total = len(lines)

        if start_line is not None or end_line is not None:
            # Line numbers are 1-indexed, inclusive on both ends.
            start = max(1, start_line or 1)
            end = min(total, end_line or total)
            if start > total:
                return f"Error: start_line={start} exceeds file length ({total} lines)."
            if start > end:
                start, end = end, start   # swap silently rather than error
            selected = lines[start - 1 : end]
            content = "".join(f"{start + i:4d}  {line}" for i, line in enumerate(selected))
            return f"[Lines {start}–{end} of {total} in '{path}']\n{content}"

        # Full file: apply line limit and suggest alternatives for large files.
        if total > _READ_LINE_LIMIT:
            content = "".join(
                f"{i + 1:4d}  {line}" for i, line in enumerate(lines[:_READ_LINE_LIMIT])
            )
            return (
                f"{content}\n"
                f"[Showing lines 1–{_READ_LINE_LIMIT} of {total}. "
                f"Use start_line/end_line for a specific range, "
                f"or get_file_outline to see the file's structure first.]"
            )

        return "".join(f"{i + 1:4d}  {line}" for i, line in enumerate(lines))
    except PermissionError:
        return f"Error: permission denied reading '{path}'."
    except Exception as e:
        return f"Error reading '{path}': {e}"


# ─── write_file ───────────────────────────────────────────────────────────────

def _check_python_syntax(path: str, content: str) -> Optional[str]:
    """Return a human-readable error if content is invalid Python, else None."""
    if not path.endswith(".py"):
        return None
    try:
        ast.parse(content)
        return None
    except SyntaxError as e:
        return f"Python syntax error at line {e.lineno}: {e.msg}"


def write_file(
    path: str,
    content: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        if start_line is not None or end_line is not None:
            if not p.exists():
                return f"Error: cannot partial-write '{path}': file does not exist."
            existing = p.read_text(encoding="utf-8")
            old_lines = existing.splitlines(keepends=True)
            total = len(old_lines)
            start = max(1, start_line or 1) - 1   # 0-indexed inclusive start
            end = min(total, end_line or total)    # 0-indexed exclusive end
            if start >= total:
                return f"Error: start_line={start + 1} exceeds file length ({total} lines)."
            new_lines = content.splitlines(keepends=True)
            # Ensure the replacement block ends with a newline when the original did.
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            merged = old_lines[:start] + new_lines + old_lines[end:]
            final_content = "".join(merged)
            syntax_err = _check_python_syntax(path, final_content)
            if syntax_err:
                return f"Error: write rejected — {syntax_err}. Fix the indentation/syntax and retry."
            p.write_text(final_content, encoding="utf-8")
            return (
                f"Replaced lines {start + 1}–{end} of '{path}' "
                f"({len(new_lines)} new lines, was {end - start})."
            )

        syntax_err = _check_python_syntax(path, content)
        if syntax_err:
            return f"Error: write rejected — {syntax_err}. Fix the indentation/syntax and retry."
        p.write_text(content, encoding="utf-8")
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"Wrote {len(content):,} chars ({lines} lines) to '{path}'."
    except PermissionError:
        return f"Error: permission denied writing '{path}'."
    except Exception as e:
        return f"Error writing '{path}': {e}"


# ─── list_directory ───────────────────────────────────────────────────────────

def list_directory(path: str = ".") -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"Error: '{path}' does not exist."
        if not p.is_dir():
            return f"Error: '{path}' is not a directory."
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        if not entries:
            return f"Directory '{path}' is empty."
        lines = [f"{path}/"]
        for entry in entries:
            if entry.is_dir():
                lines.append(f"  {entry.name}/")
            else:
                lines.append(f"  {entry.name}  ({entry.stat().st_size:,} bytes)")
        return "\n".join(lines)
    except PermissionError:
        return f"Error: permission denied listing '{path}'."
    except Exception as e:
        return f"Error listing '{path}': {e}"


# ─── run_shell ────────────────────────────────────────────────────────────────

def run_shell(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(result.stderr)
        output = "".join(parts).strip()
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output or "[command completed with no output]"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s."
    except Exception as e:
        return f"Error running command: {e}"


# ─── get_file_outline ─────────────────────────────────────────────────────────

def get_file_outline(path: str) -> str:
    """Return the symbol outline for a source file (delegates to outline.py)."""
    return get_outline(path)


# ─── search_code ─────────────────────────────────────────────────────────────

def search_code(pattern: str, path: str = ".", file_glob: str = "*") -> str:
    """Search for a regex/text pattern across source files.

    Uses ripgrep (rg) when available on PATH for speed; falls back to a pure
    Python implementation so the tool always works without rg installed.
    """
    rg_result = _search_rg(pattern, path, file_glob)
    if rg_result is not None:
        return rg_result
    return _search_python(pattern, path, file_glob)


def _search_rg(pattern: str, path: str, file_glob: str) -> Optional[str]:
    """Try ripgrep; return None if rg is not installed or exits with an error."""
    try:
        cmd = [
            "rg", "--line-number", "--no-heading", "--color=never",
            "--glob", file_glob,
            pattern, path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            lines = [ln for ln in result.stdout.strip().splitlines() if ln]
            if not lines:
                return "[no matches found]"
            extra = ""
            if len(lines) > _MAX_SEARCH_RESULTS:
                extra = f"\n[{len(lines)} matches — showing first {_MAX_SEARCH_RESULTS}]"
                lines = lines[:_MAX_SEARCH_RESULTS]
            return "\n".join(lines) + extra
        if result.returncode == 1:
            return "[no matches found]"
        return None   # rg error — fall through to Python impl
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _search_python(pattern: str, path: str, file_glob: str) -> str:
    """Pure-Python fallback using glob + re."""
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex pattern — {e}"

    base = Path(path)
    if not base.exists():
        return f"Error: path '{path}' does not exist."

    ignore_rules = IgnoreRules.load(base)

    # Build a sensible rglob pattern from the user's file_glob
    glob_pattern = f"**/{file_glob}" if ("/" not in file_glob and file_glob != "*") else file_glob
    _BINARY_EXTENSIONS = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin", ".png",
                          ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz"}
    matches: list[str] = []

    try:
        for filepath in sorted(base.rglob(glob_pattern if file_glob != "*" else "**/*")):
            if not filepath.is_file():
                continue
            if ignore_rules.is_ignored(filepath, base):
                continue
            if filepath.suffix.lower() in _BINARY_EXTENSIONS:
                continue
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if compiled.search(line):
                    try:
                        rel = filepath.relative_to(base)
                    except ValueError:
                        rel = filepath
                    matches.append(f"{rel}:{i}:{line.strip()}")
                    if len(matches) >= _MAX_SEARCH_RESULTS:
                        break
            if len(matches) >= _MAX_SEARCH_RESULTS:
                break
    except Exception as e:
        return f"Error during search: {e}"

    if not matches:
        return "[no matches found]"

    result = "\n".join(matches)
    if len(matches) == _MAX_SEARCH_RESULTS:
        result += (
            f"\n[showing first {_MAX_SEARCH_RESULTS} matches — "
            f"refine your pattern or use file_glob to narrow the search]"
        )
    return result
