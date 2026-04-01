"""Tests for minion/tools/outline.py — language-specific outline extraction.

Tests operate on source strings directly, not files on disk, so they are fast
and have no filesystem side-effects. File-path integration is covered in
test_tools_phase4.py.
"""

import pytest
from minion.tools.outline import (
    PythonOutliner,
    JSTSOutliner,
    OutlineItem,
    OUTLINER_REGISTRY,
    get_outline,
)


# ─── PythonOutliner ───────────────────────────────────────────────────────────

class TestPythonOutliner:
    def _extract(self, source: str) -> list[OutlineItem]:
        return PythonOutliner().extract(source)

    def test_extracts_top_level_function(self):
        items = self._extract("def greet(name):\n    pass\n")
        assert len(items) == 1
        assert items[0].name == "greet"
        assert items[0].kind == "function"
        assert items[0].line == 1

    def test_extracts_async_function(self):
        items = self._extract("async def fetch():\n    pass\n")
        assert len(items) == 1
        assert items[0].name == "fetch"
        assert items[0].kind == "function"

    def test_extracts_class(self):
        items = self._extract("class Foo:\n    pass\n")
        assert any(i.name == "Foo" and i.kind == "class" for i in items)

    def test_extracts_method_with_parent(self):
        source = "class Auth:\n    def login(self):\n        pass\n"
        items = self._extract(source)
        method = next((i for i in items if i.kind == "method"), None)
        assert method is not None
        assert method.name == "login"
        assert method.parent == "Auth"

    def test_class_and_method_line_numbers(self):
        source = "class Foo:\n    def bar(self):\n        pass\n"
        items = self._extract(source)
        cls = next(i for i in items if i.kind == "class")
        meth = next(i for i in items if i.kind == "method")
        assert cls.line == 1
        assert meth.line == 2

    def test_multiple_methods_in_class(self):
        source = (
            "class Manager:\n"
            "    def create(self):\n"
            "        pass\n"
            "    def delete(self):\n"
            "        pass\n"
        )
        items = self._extract(source)
        methods = [i for i in items if i.kind == "method"]
        names = {m.name for m in methods}
        assert names == {"create", "delete"}
        assert all(m.parent == "Manager" for m in methods)

    def test_multiple_top_level_functions(self):
        source = "def foo():\n    pass\ndef bar():\n    pass\n"
        items = self._extract(source)
        names = [i.name for i in items]
        assert "foo" in names
        assert "bar" in names

    def test_handles_syntax_error_gracefully(self):
        items = self._extract("def broken(")
        assert items == []

    def test_returns_empty_for_blank_file(self):
        assert self._extract("") == []

    def test_items_sorted_by_line(self):
        source = (
            "def alpha():\n    pass\n"
            "class Beta:\n"
            "    def gamma(self):\n        pass\n"
        )
        items = self._extract(source)
        lines = [i.line for i in items]
        assert lines == sorted(lines)

    def test_nested_functions_not_included(self):
        source = (
            "def outer():\n"
            "    def inner():\n"
            "        pass\n"
        )
        items = self._extract(source)
        # inner() is a nested function — we don't recurse into functions
        assert not any(i.name == "inner" for i in items)


# ─── JSTSOutliner ─────────────────────────────────────────────────────────────

class TestJSTSOutliner:
    def _extract(self, source: str) -> list[OutlineItem]:
        return JSTSOutliner().extract(source)

    def test_extracts_function_declaration(self):
        items = self._extract("function greet(name) {\n  return name;\n}\n")
        assert any(i.name == "greet" and i.kind == "function" for i in items)

    def test_extracts_async_function(self):
        items = self._extract("async function fetchData() {\n}\n")
        assert any(i.name == "fetchData" for i in items)

    def test_extracts_export_function(self):
        items = self._extract("export function helper() {}\n")
        assert any(i.name == "helper" for i in items)

    def test_extracts_const_arrow_function(self):
        items = self._extract("const doWork = () => {\n};\n")
        assert any(i.name == "doWork" for i in items)

    def test_extracts_const_function_expression(self):
        items = self._extract("const process = function() {\n};\n")
        assert any(i.name == "process" for i in items)

    def test_extracts_class_declaration(self):
        items = self._extract("class UserService {\n}\n")
        assert any(i.name == "UserService" and i.kind == "class" for i in items)

    def test_extracts_export_class(self):
        items = self._extract("export class AuthManager {\n}\n")
        assert any(i.name == "AuthManager" and i.kind == "class" for i in items)

    def test_extracts_method_inside_class(self):
        source = "class Foo {\n  doSomething() {\n  }\n}\n"
        items = self._extract(source)
        method = next((i for i in items if i.kind == "method"), None)
        assert method is not None
        assert method.name == "doSomething"
        assert method.parent == "Foo"

    def test_keyword_not_extracted_as_method(self):
        source = "class Foo {\n  bar() {\n    if (x) {\n    }\n  }\n}\n"
        items = self._extract(source)
        assert not any(i.name == "if" for i in items)

    def test_returns_empty_for_blank_source(self):
        assert self._extract("") == []

    def test_handles_generator_function(self):
        items = self._extract("function* generate() {}\n")
        assert any(i.name == "generate" for i in items)


# ─── Registry ─────────────────────────────────────────────────────────────────

class TestOutlinerRegistry:
    def test_python_extension_registered(self):
        assert ".py" in OUTLINER_REGISTRY
        assert isinstance(OUTLINER_REGISTRY[".py"], PythonOutliner)

    def test_js_extensions_registered(self):
        for ext in (".js", ".ts", ".jsx", ".tsx"):
            assert ext in OUTLINER_REGISTRY
            assert isinstance(OUTLINER_REGISTRY[ext], JSTSOutliner)


# ─── get_outline (integration) ────────────────────────────────────────────────

class TestGetOutline:
    def test_returns_outline_for_python_file(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def hello():\n    pass\n")
        result = get_outline(str(f))
        assert "hello" in result
        assert "line 1" in result

    def test_returns_outline_for_ts_file(self, tmp_path):
        f = tmp_path / "service.ts"
        f.write_text("export class MyService {\n  fetch() {}\n}\n")
        result = get_outline(str(f))
        assert "MyService" in result

    def test_unsupported_extension_returns_fallback(self, tmp_path):
        f = tmp_path / "code.rb"
        f.write_text("def hello; end")
        result = get_outline(str(f))
        assert "No outline available" in result
        assert ".rb" in result

    def test_missing_file_returns_error(self, tmp_path):
        result = get_outline(str(tmp_path / "ghost.py"))
        assert result.startswith("Error:")
        assert "does not exist" in result

    def test_empty_file_returns_no_symbols_message(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        result = get_outline(str(f))
        assert "No symbols found" in result

    def test_returns_error_for_directory(self, tmp_path):
        """Passing a directory path (not a file) returns an error string."""
        result = get_outline(str(tmp_path))
        assert result.startswith("Error:")
        assert "not a file" in result

    def test_outline_truncated_when_symbol_count_exceeds_limit(self, tmp_path):
        """Outlines beyond _MAX_OUTLINE_LINES are truncated with a notice."""
        from unittest.mock import patch
        # Generate a file with many functions
        source = "\n".join(f"def func{i}():\n    pass\n" for i in range(50))
        f = tmp_path / "many_funcs.py"
        f.write_text(source)
        with patch("minion.tools.outline._MAX_OUTLINE_LINES", 10):
            result = get_outline(str(f))
        assert "truncated" in result
