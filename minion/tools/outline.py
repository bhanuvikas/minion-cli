"""Language-specific code outline extractors.

An outline is a list of top-level symbols (classes, functions, methods) with
line numbers. It lets the model navigate to the right code without reading
entire files.

Design:
  - Outliner ABC — one subclass per language/group
  - OUTLINER_REGISTRY — maps file extensions to Outliner instances
  - get_outline(path) — public entry point; dispatches by extension

Adding a new language:
  1. Write a class that subclasses Outliner and implements extract()
  2. Register it in OUTLINER_REGISTRY with the relevant extensions

Current support:
  Python  — stdlib ast module (exact, handles nested classes/methods/async)
  JS/TS   — regex + brace-depth state machine (handles classes, functions,
             arrow functions, methods; good enough for 90%+ of real-world code)

Deferred:
  Tree-sitter — universal parser, 50+ languages, no dependencies on grammars
                being pre-compiled. Add as a new Outliner subclass when needed.
"""

import ast
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_MAX_OUTLINE_LINES = 200   # cap returned lines so outlines stay token-compact


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class OutlineItem:
    name: str
    kind: str               # "class" | "function" | "method"
    line: int
    parent: Optional[str] = None   # class name for methods


# ─── Abstract base ────────────────────────────────────────────────────────────

class Outliner(ABC):
    @abstractmethod
    def extract(self, source: str) -> list[OutlineItem]:
        """Parse source and return a list of OutlineItems, sorted by line."""
        ...


# ─── Python (stdlib ast) ──────────────────────────────────────────────────────

class PythonOutliner(Outliner):
    """Uses Python's ast module for exact, reliable symbol extraction."""

    def extract(self, source: str) -> list[OutlineItem]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        visitor = _PythonVisitor()
        visitor.visit(tree)
        return sorted(visitor.items, key=lambda x: x.line)


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.items: list[OutlineItem] = []
        self._class_stack: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.items.append(OutlineItem(name=node.name, kind="class", line=node.lineno))
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._class_stack:
            self.items.append(OutlineItem(
                name=node.name,
                kind="method",
                line=node.lineno,
                parent=self._class_stack[-1],
            ))
        else:
            self.items.append(OutlineItem(name=node.name, kind="function", line=node.lineno))
        # Do not recurse — nested functions are too noisy

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]


# ─── JavaScript / TypeScript (regex + brace depth) ────────────────────────────

class JSTSOutliner(Outliner):
    """Regex-based outliner for JS/TS.

    Handles:
      - class Foo / export class Foo / export default class Foo
      - function foo() / export function foo() / async function foo()
      - const foo = () => / const foo = function() (top level)
      - Methods inside class bodies (detected by indentation + brace tracking)

    Limitation: brace counting is fooled by braces in strings/comments,
    which can shift method attribution. For Phase 4 this is acceptable.
    Tree-sitter (deferred) solves this correctly.
    """

    _CLASS_RE = re.compile(
        r'^\s*(?:export\s+(?:default\s+)?)?class\s+(\w+)'
    )
    _FUNC_RE = re.compile(
        r'^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\('
    )
    _CONST_FUNC_RE = re.compile(
        r'^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*='
        r'\s*(?:async\s+)?(?:function\b|\()'
    )
    _METHOD_RE = re.compile(
        r'^(\s{2,}|\t)(?:(?:static|async|get|set|public|private|protected|override)\s+)*'
        r'(\w+)\s*\('
    )
    _KEYWORDS = frozenset({
        "if", "for", "while", "switch", "catch", "do", "return",
        "new", "typeof", "instanceof", "else", "case", "throw",
    })

    def extract(self, source: str) -> list[OutlineItem]:
        items: list[OutlineItem] = []
        brace_depth = 0
        current_class: Optional[str] = None
        class_open_depth = 0

        for lineno, raw_line in enumerate(source.splitlines(), 1):
            depth_before = brace_depth
            # Count braces (imprecise — ignores strings/comments, acceptable here)
            brace_depth += raw_line.count("{") - raw_line.count("}")

            # ── Class declaration ──────────────────────────────────────────
            m = self._CLASS_RE.match(raw_line)
            if m:
                current_class = m.group(1)
                class_open_depth = depth_before
                items.append(OutlineItem(name=m.group(1), kind="class", line=lineno))
                continue

            # ── Top-level function declaration ────────────────────────────
            m = self._FUNC_RE.match(raw_line)
            if m and current_class is None:
                items.append(OutlineItem(name=m.group(1), kind="function", line=lineno))
                continue

            # ── Top-level const = function/arrow ──────────────────────────
            m = self._CONST_FUNC_RE.match(raw_line)
            if m and current_class is None:
                items.append(OutlineItem(name=m.group(1), kind="function", line=lineno))
                continue

            # ── Method inside class body ──────────────────────────────────
            if current_class is not None:
                m = self._METHOD_RE.match(raw_line)
                if m and m.group(2) not in self._KEYWORDS:
                    items.append(OutlineItem(
                        name=m.group(2),
                        kind="method",
                        line=lineno,
                        parent=current_class,
                    ))

            # ── Exit class body when brace depth returns to pre-class level ─
            if current_class is not None and brace_depth <= class_open_depth:
                current_class = None

        return items


# ─── Registry and public entry point ──────────────────────────────────────────

OUTLINER_REGISTRY: dict[str, Outliner] = {
    ".py":  PythonOutliner(),
    ".js":  JSTSOutliner(),
    ".ts":  JSTSOutliner(),
    ".jsx": JSTSOutliner(),
    ".tsx": JSTSOutliner(),
    ".mjs": JSTSOutliner(),
    ".cjs": JSTSOutliner(),
}


def get_outline(path: str) -> str:
    """Return a formatted outline string for the given file.

    Falls back to a helpful message for unsupported file types.
    """
    p = Path(path)
    if not p.exists():
        return f"Error: '{path}' does not exist."
    if not p.is_file():
        return f"Error: '{path}' is not a file."

    ext = p.suffix.lower()
    outliner = OUTLINER_REGISTRY.get(ext)
    if outliner is None:
        return (
            f"No outline available for '{ext}' files — "
            f"use read_file to inspect '{path}' directly."
        )

    try:
        source = p.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return f"Error: permission denied reading '{path}'."
    except Exception as e:
        return f"Error reading '{path}': {e}"

    items = outliner.extract(source)
    if not items:
        return f"No symbols found in '{path}' (file may be empty or contain only imports)."

    return _format_outline(path, items)


def _format_outline(path: str, items: list[OutlineItem]) -> str:
    """Format outline items as an indented, line-numbered listing."""
    lines = [f"Outline of {path}:"]
    for item in items:
        if item.kind == "class":
            lines.append(f"  class {item.name} (line {item.line})")
        elif item.kind == "method":
            lines.append(f"      {item.name} (line {item.line})")
        else:
            lines.append(f"  {item.name} (line {item.line})")

        if len(lines) > _MAX_OUTLINE_LINES:
            lines.append(f"  ... (outline truncated at {_MAX_OUTLINE_LINES} entries)")
            break

    return "\n".join(lines)
