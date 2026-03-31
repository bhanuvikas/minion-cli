"""Tool implementations — pure Python functions that execute tool calls.

Each function:
  - Takes keyword arguments matching its tool schema in definitions.py
  - Returns a plain string (the observation injected back into LLM context)
  - Never raises — all errors are caught and returned as descriptive strings
    so the model can reason about failures and recover gracefully

No UX or confirmation logic lives here. That's executor.py's responsibility.
"""

import subprocess
from pathlib import Path

DEFAULT_MAX_BYTES = 50_000
DEFAULT_TIMEOUT = 30


def read_file(path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"Error: '{path}' does not exist."
        if not p.is_file():
            return f"Error: '{path}' is not a file."
        raw = p.read_bytes()
        text = raw[:max_bytes].decode("utf-8", errors="replace")
        if len(raw) > max_bytes:
            text += f"\n\n[truncated — file is {len(raw):,} bytes, showing first {max_bytes:,}]"
        return text
    except PermissionError:
        return f"Error: permission denied reading '{path}'."
    except Exception as e:
        return f"Error reading '{path}': {e}"


def write_file(path: str, content: str) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"Wrote {len(content):,} chars ({lines} lines) to '{path}'."
    except PermissionError:
        return f"Error: permission denied writing '{path}'."
    except Exception as e:
        return f"Error writing '{path}': {e}"


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
                size = entry.stat().st_size
                lines.append(f"  {entry.name}  ({size:,} bytes)")
        return "\n".join(lines)
    except PermissionError:
        return f"Error: permission denied listing '{path}'."
    except Exception as e:
        return f"Error listing '{path}': {e}"


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
