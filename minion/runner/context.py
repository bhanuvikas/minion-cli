"""Prompt context assembly — @mention resolution, message serialization."""

import re
from pathlib import Path


# Matches @path patterns that contain at least one / or a file extension.
# Examples: @src/auth.py  @README.md  @config/settings.ts
# Does NOT match bare @property, @classmethod (no slash or extension dot).
_MENTION_RE = re.compile(
    r"@("
    r"(?:\w[\w\-]*/)+[\w.\-]+"           # path/with/dirs/file  e.g. @src/auth.py
    r"|[\w][\w\-]*\.[\w]+(?:\.[\w]+)*"   # bare word.ext        e.g. @README.md
    r"|\.[a-zA-Z][\w\-]*(?:\.[\w]+)*"    # bare dotfile         e.g. @.gitignore, @.env.example
    r")"
)


def _resolve_mentions(prompt: str, cwd: Path) -> str:
    """Expand @file.py references by appending file contents to the prompt.

    Preserves the original mention text inline so the model sees what the
    user typed, then appends the actual file contents at the end.
    Deduplicates repeated mentions of the same file.
    """
    mentions = list(dict.fromkeys(_MENTION_RE.findall(prompt)))  # unique, ordered
    if not mentions:
        return prompt

    appended: list[str] = []
    for mention_path in mentions:
        p = cwd / mention_path
        if not p.exists():
            appended.append(f"[@{mention_path}: file not found]")
        elif not p.is_file():
            appended.append(f"[@{mention_path}: not a file — cannot inject]")
        else:
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                appended.append(f"[Contents of {mention_path}]\n{content}")
            except Exception as e:
                appended.append(f"[@{mention_path}: error reading file — {e}]")

    if not appended:
        return prompt
    return prompt + "\n\n" + "\n\n".join(appended)


def _serialize_messages(messages) -> list:
    """Convert conversation messages to a JSON-serializable list for tracing."""
    import dataclasses
    result = []
    for msg in messages:
        content = msg.content
        if isinstance(content, str):
            content_out = content
        elif isinstance(content, list):
            content_out = []
            for block in content:
                try:
                    content_out.append(dataclasses.asdict(block))
                except Exception:
                    content_out.append(str(block))
        else:
            content_out = str(content)
        result.append({"role": msg.role, "content": content_out})
    return result


def _snapshot_messages(messages) -> list[dict]:
    """Snapshot conversation messages as plain dicts for the subagent inspector.

    Converts SDK content blocks to simple dicts so the result is safe to store
    across threads without holding references to live SDK objects.
    """
    out: list[dict] = []
    for m in messages:
        if isinstance(m.content, str):
            out.append({"role": m.role, "type": "text", "text": m.content})
        elif isinstance(m.content, list):
            blocks: list[dict] = []
            for b in m.content:
                if hasattr(b, "text") and not hasattr(b, "name"):      # TextBlock
                    blocks.append({"type": "text", "text": b.text})
                elif hasattr(b, "name") and hasattr(b, "input"):       # ToolUseBlock
                    blocks.append({"type": "tool_use", "name": b.name, "input": dict(b.input)})
                elif hasattr(b, "tool_use_id"):                        # ToolResultBlock
                    rc = b.content if isinstance(b.content, str) else str(b.content)
                    blocks.append({"type": "tool_result", "tool_use_id": b.tool_use_id, "content": rc})
            out.append({"role": m.role, "type": "blocks", "blocks": blocks})
    return out
