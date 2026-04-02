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


def format_diff_rich(
    original: str,
    revised: str,
    context_lines: int = 3,
) -> str:
    """Produce a Rich-markup string showing the diff between original and revised.

    Additions:  [bold green]+ line[/bold green]
    Removals:   [bold red]- line[/bold red]
    Context:    [dim]  line[/dim]

    Uses unified_diff internally for context-aware hunks (shows only the
    neighbourhood of each change, not the entire file). Returns an empty
    string when the two inputs are identical.

    The returned string is safe to pass to Rich's console.print() — all
    diff content is plain text that does not contain Rich markup characters.
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

    for line in diff_lines:
        if line.startswith("---") or line.startswith("+++"):
            # Skip file header lines — not useful in a terminal chat context
            continue
        elif line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                orig_lineno = int(m.group(1))
                new_lineno = int(m.group(2))
            parts.append(f"[dim]{_escape_rich(line)}[/dim]")
        elif line.startswith("+"):
            parts.append(f"[bold green]{new_lineno:>4}  {_escape_rich(line)}[/bold green]")
            new_lineno += 1
        elif line.startswith("-"):
            parts.append(f"[bold red]{orig_lineno:>4}  {_escape_rich(line)}[/bold red]")
            orig_lineno += 1
        else:
            parts.append(f"[dim]{orig_lineno:>4}  {_escape_rich(line)}[/dim]")
            orig_lineno += 1
            new_lineno += 1

    return "\n".join(parts)


def _escape_rich(text: str) -> str:
    """Escape Rich markup special characters in diff content.

    Diff lines can contain square brackets (e.g. list literals, imports)
    which Rich would otherwise interpret as markup tags.
    """
    return text.replace("[", "\\[")
