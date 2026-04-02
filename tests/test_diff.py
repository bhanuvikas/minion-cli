"""Tests for minion/diff.py — compute_diff and format_diff_rich.

No API calls. No filesystem operations. Pure unit tests.
"""

import sys
import pytest

from minion.diff import compute_diff, format_diff_rich


# ─── compute_diff ─────────────────────────────────────────────────────────────

class TestComputeDiff:
    def test_identical_strings_return_empty(self):
        assert compute_diff("hello", "hello") == []

    def test_empty_strings_return_empty(self):
        assert compute_diff("", "") == []

    def test_single_line_change_detected(self):
        result = compute_diff("old line", "new line")
        tags = [tag for tag, _ in result]
        assert "-" in tags
        assert "+" in tags

    def test_addition_only(self):
        result = compute_diff("line1", "line1\nline2")
        tags = [tag for tag, _ in result]
        assert "+" in tags
        assert "-" not in tags

    def test_removal_only(self):
        result = compute_diff("line1\nline2", "line1")
        tags = [tag for tag, _ in result]
        assert "-" in tags
        assert "+" not in tags

    def test_multiline_diff_preserves_order(self):
        original = "a\nb\nc"
        revised = "a\nB\nc"
        result = compute_diff(original, revised)
        lines = [line for _, line in result]
        # 'a' and 'c' should appear as context; 'b' removed; 'B' added
        assert "a" in lines
        assert "c" in lines
        assert "b" in lines
        assert "B" in lines

    def test_returns_list_of_tuples(self):
        result = compute_diff("x", "y")
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert item[0] in ("+", "-", " ")


# ─── format_diff_rich ─────────────────────────────────────────────────────────

class TestFormatDiffRich:
    def test_empty_diff_returns_empty_string(self):
        assert format_diff_rich("same", "same") == ""

    def test_additions_get_green_markup(self):
        result = format_diff_rich("line1", "line1\nline2")
        assert "[bold green]" in result
        assert "line2" in result

    def test_removals_get_red_markup(self):
        result = format_diff_rich("line1\nline2", "line1")
        assert "[bold red]" in result

    def test_context_lines_get_dim_markup(self):
        original = "a\nb\nc\nd\ne"
        revised = "a\nb\nC\nd\ne"
        result = format_diff_rich(original, revised, context_lines=1)
        # context lines ('b', 'd') should be dim
        assert "[dim]" in result

    def test_returns_string(self):
        result = format_diff_rich("old", "new")
        assert isinstance(result, str)

    def test_square_brackets_in_content_are_escaped(self):
        """Diff content containing [ characters must not break Rich markup."""
        original = "x = [1, 2, 3]"
        revised = "x = [1, 2, 3, 4]"
        result = format_diff_rich(original, revised)
        # The brackets in the code should be escaped (\\[) so Rich doesn't
        # misinterpret them as markup tags.
        assert "\\[" in result

    def test_no_minion_package_imports(self):
        """diff.py must not import from the minion package (standalone guarantee)."""
        import importlib
        import minion.diff as diff_module
        import inspect
        source = inspect.getsource(diff_module)
        # The only allowed import pattern from within the package would start with
        # "from ."; the module must have none.
        assert "from ." not in source
        assert "from minion" not in source
