"""Tests for minion/diff.py — compute_diff and format_diff_rich.

No API calls. No filesystem operations. Pure unit tests.
"""

import sys
import pytest

from minion.output.diff import compute_diff, format_diff_rich, _inline_diff_markup


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

    def test_additions_get_green_background(self):
        result = format_diff_rich("line1", "line1\nline2")
        assert "on #004a00" in result
        assert "line2" in result

    def test_removals_get_red_background(self):
        result = format_diff_rich("line1\nline2", "line1")
        assert "on #4a0000" in result

    def test_context_lines_get_dim_markup(self):
        original = "a\nb\nc\nd\ne"
        revised = "a\nb\nC\nd\ne"
        result = format_diff_rich(original, revised, context_lines=1)
        assert "[dim]" in result

    def test_returns_string(self):
        result = format_diff_rich("old", "new")
        assert isinstance(result, str)

    def test_square_brackets_in_content_are_escaped(self):
        """Diff content containing [ characters must not break Rich markup."""
        original = "x = [1, 2, 3]"
        revised = "x = [1, 2, 3, 4]"
        result = format_diff_rich(original, revised)
        assert "\\[" in result

    def test_line_numbers_present_in_output(self):
        original = "line1\nline2\nline3"
        revised = "line1\nLINE2\nline3"
        result = format_diff_rich(original, revised)
        assert "1" in result
        assert "2" in result

    def test_removed_line_shows_original_lineno(self):
        original = "a\nb\nc"
        revised = "a\nc"
        result = format_diff_rich(original, revised, context_lines=0)
        removed_lines = [p for p in result.split("\n") if "on #4a0000" in p]
        assert any("2" in p for p in removed_lines)

    def test_added_line_shows_new_lineno(self):
        original = "a\nc"
        revised = "a\nb\nc"
        result = format_diff_rich(original, revised, context_lines=0)
        added_lines = [p for p in result.split("\n") if "on #004a00" in p]
        assert any("2" in p for p in added_lines)

    def test_replacement_pair_gets_inline_diff(self):
        """Adjacent -/+ lines (replacements) should have word-level highlights."""
        result = format_diff_rich("hello world", "hello earth", context_lines=0)
        # The changed word should get a brighter inline highlight
        assert "on #8b0000" in result   # removed word highlight
        assert "on #006400" in result   # added word highlight

    def test_pure_addition_no_inline_diff(self):
        """A pure addition (no adjacent removal) gets line bg only, no word highlights."""
        result = format_diff_rich("a\nb", "a\nb\nc", context_lines=0)
        assert "on #004a00" in result
        assert "on #006400" not in result

    def test_pure_removal_no_inline_diff(self):
        """A pure removal (no adjacent addition) gets line bg only, no word highlights."""
        result = format_diff_rich("a\nb\nc", "a\nb", context_lines=0)
        assert "on #4a0000" in result
        assert "on #8b0000" not in result


# ─── _inline_diff_markup ──────────────────────────────────────────────────────

class TestInlineDiffMarkup:
    def test_equal_text_has_no_highlights(self):
        old_hl, new_hl = _inline_diff_markup("hello", "hello")
        assert "on #" not in old_hl
        assert "on #" not in new_hl

    def test_changed_word_gets_highlight(self):
        old_hl, new_hl = _inline_diff_markup("hello world", "hello earth")
        assert "on #8b0000" in old_hl   # "world" highlighted in old
        assert "on #006400" in new_hl   # "earth" highlighted in new

    def test_unchanged_prefix_not_highlighted(self):
        old_hl, _ = _inline_diff_markup("hello world", "hello earth")
        # "hello " is unchanged — should not be inside a highlight span
        assert old_hl.startswith("hello ")

    def test_pure_insertion_only_in_new(self):
        old_hl, new_hl = _inline_diff_markup("hi", "hi there")
        assert "on #" not in old_hl
        assert "on #006400" in new_hl

    def test_pure_deletion_only_in_old(self):
        old_hl, new_hl = _inline_diff_markup("hi there", "hi")
        assert "on #8b0000" in old_hl
        assert "on #" not in new_hl

    def test_no_minion_package_imports(self):
        """output/diff.py must not import from the minion package (standalone guarantee)."""
        import minion.output.diff as diff_module
        import inspect
        source = inspect.getsource(diff_module)
        # The only allowed import pattern from within the package would start with
        # "from ."; the module must have none.
        assert "from ." not in source
        assert "from minion" not in source
