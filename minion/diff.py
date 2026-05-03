"""Diff utility — compute and format diffs between two text blobs.

Single responsibility: produce Rich-markup diff strings for console display.
No imports from any minion.* module — intentionally standalone so this module
can be reused in Phase 7 (plan revision diffs) and Phase 8 (skill output
comparisons) without coupling.

Usage (from theme.py or any caller):
    markup = format_diff_rich(original, revised)
    console.print(markup)
"""

import difflib
import re
from typing import Optional

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Line background colors (dark, terminal-friendly)
_REMOVED_LINE_STYLE = "on #3b1111"
_ADDED_LINE_STYLE   = "on #113b11"
# Inline word highlight colors (brighter, within the line background)
_REMOVED_WORD_STYLE = "on #6b2020"
_ADDED_WORD_STYLE   = "on #1f6b1f"


def compute_diff(original: str, revised: str) -> list[tuple[str, str]]:
    """Return a list of (tag, line) pairs from a line-by-line diff.

    tag values:
      '+'  — line added in revised
      '-'  — line removed from original
      ' '  — context line (unchanged)

    Returns an empty list when original == revised.
    """
    if original == revised:
        return []

    original_lines = original.splitlines()
    revised_lines = revised.splitlines()

    result: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(None, original_lines, revised_lines)

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for line in original_lines[i1:i2]:
                result.append((" ", line))
        elif op in ("replace", "delete"):
            for line in original_lines[i1:i2]:
                result.append(("-", line))
            if op == "replace":
                for line in revised_lines[j1:j2]:
                    result.append(("+", line))
        elif op == "insert":
            for line in revised_lines[j1:j2]:
                result.append(("+", line))

    return result


def _inline_diff_markup(old: str, new: str) -> tuple[str, str]:
    """Character-level diff between old and new content (without leading +/- char).

    Returns (old_markup, new_markup) where changed character spans are wrapped
    in a brighter background so individual edits are visible within the line.
    Unchanged spans are escaped for Rich but otherwise unstyled (they inherit
    the outer line background).
    """
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    old_parts: list[str] = []
    new_parts: list[str] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            old_parts.append(_escape_rich(old[i1:i2]))
            new_parts.append(_escape_rich(new[j1:j2]))
        elif op == "replace":
            old_parts.append(f"[{_REMOVED_WORD_STYLE}]{_escape_rich(old[i1:i2])}[/]")
            new_parts.append(f"[{_ADDED_WORD_STYLE}]{_escape_rich(new[j1:j2])}[/]")
        elif op == "delete":
            old_parts.append(f"[{_REMOVED_WORD_STYLE}]{_escape_rich(old[i1:i2])}[/]")
        elif op == "insert":
            new_parts.append(f"[{_ADDED_WORD_STYLE}]{_escape_rich(new[j1:j2])}[/]")
    return "".join(old_parts), "".join(new_parts)


def format_diff_rich(
    original: str,
    revised: str,
    context_lines: int = 3,
) -> str:
    """Produce a Rich-markup string showing the diff between original and revised.

    Removed lines: dark red background; changed chars highlighted with brighter red.
    Added lines:   dark green background; changed chars highlighted with brighter green.
    Context lines: dim text, no background.

    Adjacent -/+ pairs (replacements) are character-diffed so only the exact
    changed spans are highlighted within each line, matching the Claude Code
    diff style. Pure removals and pure additions get a full-line background
    without inline highlights.

    Uses unified_diff for context-aware hunks. Returns an empty string when
    the two inputs are identical.
    """
    if original == revised:
        return ""

    original_lines = original.splitlines(keepends=False)
    revised_lines = revised.splitlines(keepends=False)

    diff_lines = list(
        difflib.unified_diff(
            original_lines,
            revised_lines,
            fromfile="original",
            tofile="refined",
            n=context_lines,
            lineterm="",
        )
    )

    if not diff_lines:
        return ""

    parts: list[str] = []
    orig_lineno = 0
    new_lineno = 0
    # Buffer consecutive - lines; paired with subsequent + lines for inline diff
    pending: list[tuple[int, str]] = []

    def flush_pending() -> None:
        """Render buffered removals that had no matching addition."""
        for lineno, content in pending:
            parts.append(f"[{_REMOVED_LINE_STYLE}]{lineno:>4}   {_escape_rich(content)}[/]")
        pending.clear()

    for line in diff_lines:
        if line.startswith("---") or line.startswith("+++"):
            continue
        elif line.startswith("@@"):
            flush_pending()
            m = _HUNK_RE.match(line)
            if m:
                orig_lineno = int(m.group(1))
                new_lineno = int(m.group(2))
            parts.append(f"[dim]{_escape_rich(line)}[/dim]")
        elif line.startswith("-"):
            pending.append((orig_lineno, line[1:]))
            orig_lineno += 1
        elif line.startswith("+"):
            content = line[1:]
            if pending:
                # Pair with the oldest buffered removal → inline character diff
                old_lineno, old_content = pending.pop(0)
                old_hl, new_hl = _inline_diff_markup(old_content, content)
                parts.append(f"[{_REMOVED_LINE_STYLE}]{old_lineno:>4}   {old_hl}[/]")
                parts.append(f"[{_ADDED_LINE_STYLE}]{new_lineno:>4}   {new_hl}[/]")
            else:
                # Pure addition — no removal to pair with
                parts.append(f"[{_ADDED_LINE_STYLE}]{new_lineno:>4}   {_escape_rich(content)}[/]")
            new_lineno += 1
        else:
            flush_pending()
            content = line[1:] if line else line
            parts.append(f"[dim]{orig_lineno:>4}   {_escape_rich(content)}[/dim]")
            orig_lineno += 1
            new_lineno += 1

    flush_pending()
    return "\n".join(parts)


def _escape_rich(text: str) -> str:
    """Escape Rich markup special characters in diff content.

    Diff lines can contain square brackets (e.g. list literals, imports)
    which Rich would otherwise interpret as markup tags.
    """
    return text.replace("[", "\\[")
