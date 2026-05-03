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
_MAX_GLOB_RESULTS = 200
_WEB_FETCH_TIMEOUT = 15
_WEB_FETCH_MAX_CHARS = 50_000


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


# ─── edit_file helpers ────────────────────────────────────────────────────────

def _whitespace_flexible_replace(content: str, old_string: str, new_string: str) -> Optional[str]:
    """Fallback replace with flexible leading-whitespace matching.

    Strips leading whitespace from each line during comparison. When a unique
    match is found, computes the indentation delta from the first non-empty line
    and applies it uniformly to all lines of new_string.
    Returns None if no match or if the match is ambiguous (multiple locations).
    """
    old_lines = old_string.splitlines()
    if not old_lines:
        return None

    content_lines = content.splitlines()
    has_trailing_newline = content.endswith("\n")
    old_stripped = [line.lstrip() for line in old_lines]
    n = len(old_lines)

    matches: list[int] = []
    for i in range(len(content_lines) - n + 1):
        if all(content_lines[i + j].lstrip() == old_stripped[j] for j in range(n)):
            matches.append(i)

    if len(matches) != 1:
        return None

    start = matches[0]

    # Compute indentation delta from the first non-empty paired line
    indent_delta = 0
    for j in range(n):
        if content_lines[start + j].strip() and old_lines[j].strip():
            file_indent = len(content_lines[start + j]) - len(content_lines[start + j].lstrip())
            orig_indent = len(old_lines[j]) - len(old_lines[j].lstrip())
            indent_delta = file_indent - orig_indent
            break

    new_lines = new_string.splitlines()
    adjusted: list[str] = []
    for line in new_lines:
        if line.strip() and indent_delta != 0:
            current_indent = len(line) - len(line.lstrip())
            new_indent = max(0, current_indent + indent_delta)
            adjusted.append(" " * new_indent + line.lstrip())
        else:
            adjusted.append(line)

    result_lines = content_lines[:start] + adjusted + content_lines[start + n:]
    result = "\n".join(result_lines)
    if has_trailing_newline:
        result += "\n"
    return result


def _apply_edit(content: str, old_string: str, new_string: str) -> str:
    """Return the new file content after replacing old_string with new_string.

    Returns a string starting with 'Error:' if the replacement cannot be applied.
    Used by both edit_file (to write) and executor.py (to preview the diff).
    """
    count = content.count(old_string)
    if count == 1:
        return content.replace(old_string, new_string, 1)
    if count > 1:
        return (
            f"Error: old_string appears {count} times — "
            f"add more surrounding context lines to make it unique."
        )
    # Whitespace-flexible fallback
    result = _whitespace_flexible_replace(content, old_string, new_string)
    if result is not None:
        return result
    return (
        "Error: old_string not found in file. "
        "Make sure it matches exactly (including whitespace and indentation). "
        "Use read_file to verify the current content."
    )


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


# ─── edit_file ────────────────────────────────────────────────────────────────

def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace old_string with new_string in an existing file.

    Exact match is tried first. Falls back to whitespace-normalized matching
    when leading indentation differs but content is otherwise identical.
    Returns an error if old_string is not found or appears more than once.
    """
    try:
        p = Path(path)
        if not p.exists():
            return f"Error: '{path}' does not exist. Use write_file to create a new file."
        if not p.is_file():
            return f"Error: '{path}' is not a file."

        content = p.read_text(encoding="utf-8", errors="replace")
        result = _apply_edit(content, old_string, new_string)
        if result.startswith("Error:"):
            return f"{result} (in '{path}')"

        syntax_err = _check_python_syntax(path, result)
        if syntax_err:
            return f"Error: edit rejected — {syntax_err}. Fix the syntax and retry."

        p.write_text(result, encoding="utf-8")
        old_lines = old_string.count("\n") + 1
        new_lines = new_string.count("\n") + 1
        return f"Edited '{path}': replaced {old_lines}-line block with {new_lines} lines."
    except PermissionError:
        return f"Error: permission denied editing '{path}'."
    except Exception as e:
        return f"Error editing '{path}': {e}"


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


# ─── search_file ─────────────────────────────────────────────────────────────

def search_file(pattern: str, path: str = ".", file_glob: str = "*") -> str:
    """Search for a regex/text pattern across files.

    Uses ripgrep (rg) when available on PATH for speed; falls back to a pure
    Python implementation so the tool always works without rg installed.
    """
    rg_result = _search_rg(pattern, path, file_glob)
    if rg_result is not None:
        return rg_result
    return _search_python(pattern, path, file_glob)


# Keep the old name as an alias so existing callers and tests still work.
search_code = search_file


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


# ─── glob ─────────────────────────────────────────────────────────────────────

def glob(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern.

    Uses Python's pathlib.Path.glob() — supports ** for recursive matching.
    Respects .gitignore rules. Returns file paths relative to the search root.
    """
    try:
        base = Path(path)
        if not base.exists():
            return f"Error: path '{path}' does not exist."
        if not base.is_dir():
            return f"Error: '{path}' is not a directory."

        ignore_rules = IgnoreRules.load(base)
        matches: list[str] = []

        for p in sorted(base.glob(pattern)):
            if not p.is_file():
                continue
            if ignore_rules.is_ignored(p, base):
                continue
            try:
                rel = p.relative_to(base)
            except ValueError:
                rel = p
            matches.append(str(rel))
            if len(matches) >= _MAX_GLOB_RESULTS:
                break

        if not matches:
            return f"[no files matching '{pattern}']"

        result = "\n".join(matches)
        if len(matches) == _MAX_GLOB_RESULTS:
            result += f"\n[showing first {_MAX_GLOB_RESULTS} matches — refine your pattern]"
        return result
    except Exception as e:
        return f"Error: {e}"


# ─── web_fetch ────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """Remove HTML tags, scripts, and styles; normalise whitespace."""
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html,
                  flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<[^>]+>", "", html)
    lines = [line.strip() for line in html.splitlines()]
    out: list[str] = []
    prev_blank = False
    for line in lines:
        if not line:
            if not prev_blank:
                out.append("")
            prev_blank = True
        else:
            out.append(line)
            prev_blank = False
    return "\n".join(out).strip()


def web_fetch(url: str) -> str:
    """Fetch the content of a URL and return it as plain text.

    HTML pages are stripped of tags and scripts. Responses are truncated at
    50,000 characters so large pages don't flood the context window.
    """
    try:
        import httpx
        headers = {"User-Agent": "minion-cli/1.0 (coding assistant)"}
        with httpx.Client(timeout=_WEB_FETCH_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        text = _strip_html(response.text) if "html" in content_type else response.text

        if len(text) > _WEB_FETCH_MAX_CHARS:
            text = (text[:_WEB_FETCH_MAX_CHARS]
                    + f"\n\n[Truncated — showing {_WEB_FETCH_MAX_CHARS:,} of {len(text):,} chars]")
        return text
    except Exception as e:
        # Import httpx errors lazily so the name is available in the except clause
        try:
            import httpx as _httpx
            if isinstance(e, _httpx.TimeoutException):
                return f"Error: request to '{url}' timed out after {_WEB_FETCH_TIMEOUT}s."
            if isinstance(e, _httpx.HTTPStatusError):
                return f"Error: HTTP {e.response.status_code} from '{url}'."
        except ImportError:
            pass
        return f"Error fetching '{url}': {e}"


# ─── todo_write / todo_read ────────────────────────────────────────────────────

_TODO_LIST: list[dict] = []


def todo_write(items: list[dict]) -> str:
    global _TODO_LIST
    valid_statuses = {"pending", "in_progress", "done"}
    for item in items:
        if item.get("status", "pending") not in valid_statuses:
            return f"Error: invalid status '{item.get('status')}' — must be pending, in_progress, or done"
    _TODO_LIST = [
        {"text": str(i.get("text", "")), "status": i.get("status", "pending")}
        for i in items
    ]
    if not _TODO_LIST:
        return "Todo list cleared."
    pending = sum(1 for i in _TODO_LIST if i["status"] == "pending")
    in_prog  = sum(1 for i in _TODO_LIST if i["status"] == "in_progress")
    done     = sum(1 for i in _TODO_LIST if i["status"] == "done")
    return (
        f"Todo list updated: {len(_TODO_LIST)} item(s) — "
        f"{done} done, {in_prog} in progress, {pending} pending."
    )


def todo_read() -> str:
    if not _TODO_LIST:
        return "No tasks."
    symbol = {"done": "✓", "in_progress": "→", "pending": "○"}
    lines = [
        f"{symbol.get(i['status'], '○')} [{i['status']}] {i['text']}"
        for i in _TODO_LIST
    ]
    return "\n".join(lines)


def get_todo_list() -> list[dict]:
    return [dict(i) for i in _TODO_LIST]
