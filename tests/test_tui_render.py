"""Tests for tui/render.py — render_message_blocks."""

import pytest

from minion.tui.render import render_message_blocks


# ── helpers ──────────────────────────────────────────────────────────────────

def _styles(lines: list) -> list[list[str]]:
    """Extract just the style strings from each row."""
    return [[s for s, _ in row] for row in lines]


def _texts(lines: list) -> list[list[str]]:
    """Extract just the text strings from each row."""
    return [[t for _, t in row] for row in lines]


def _flat_text(lines: list) -> str:
    """Concatenate all text fragments across all rows."""
    return " ".join(t for row in lines for _, t in row)


# ── empty / no messages ───────────────────────────────────────────────────────

class TestEmptyMessages:
    def test_empty_list_returns_empty(self):
        assert render_message_blocks([], "coder") == []

    def test_unknown_role_is_ignored(self):
        msgs = [{"role": "system", "type": "text", "text": "ignored"}]
        assert render_message_blocks(msgs, "coder") == []


# ── user text messages ────────────────────────────────────────────────────────

class TestUserTextMessages:
    def _msg(self, text: str) -> dict:
        return {"role": "user", "type": "text", "text": text}

    def test_produces_two_rows(self):
        lines = render_message_blocks([self._msg("hello")], "agent")
        assert len(lines) == 2

    def test_first_row_has_minion_prefix(self):
        lines = render_message_blocks([self._msg("hello")], "agent")
        row_styles = _styles(lines)[0]
        assert "class:minion-prefix" in row_styles

    def test_first_row_has_text(self):
        lines = render_message_blocks([self._msg("hello")], "agent")
        assert "hello" in _flat_text([lines[0]])

    def test_second_row_is_blank(self):
        lines = render_message_blocks([self._msg("hello")], "agent")
        assert lines[1] == [("", "")]

    def test_newlines_replaced(self):
        lines = render_message_blocks([self._msg("line1\nline2")], "agent")
        text = _flat_text([lines[0]])
        assert "\n" not in text
        assert "line1" in text

    def test_truncated_in_normal_mode(self):
        long_text = "x" * 200
        lines = render_message_blocks([self._msg(long_text)], "agent", expanded=False)
        row_text = _flat_text([lines[0]])
        assert len(row_text) < 200

    def test_longer_in_expanded_mode(self):
        long_text = "x" * 200
        normal = render_message_blocks([self._msg(long_text)], "agent", expanded=False)
        exp    = render_message_blocks([self._msg(long_text)], "agent", expanded=True)
        assert len(_flat_text([exp[0]])) >= len(_flat_text([normal[0]]))


# ── assistant block messages ──────────────────────────────────────────────────

class TestAssistantBlocks:
    def _msg(self, blocks: list) -> dict:
        return {"role": "assistant", "type": "blocks", "blocks": blocks}

    def _text_blk(self, text: str) -> dict:
        return {"type": "text", "text": text}

    def _tool_blk(self, name: str, inp: dict | None = None) -> dict:
        return {"type": "tool_use", "name": name, "input": inp or {}}

    def test_text_block_produces_two_rows(self):
        lines = render_message_blocks([self._msg([self._text_blk("Hi")])], "coder")
        assert len(lines) == 2

    def test_text_block_has_label(self):
        lines = render_message_blocks([self._msg([self._text_blk("Hi")])], "coder")
        text = _flat_text([lines[0]])
        assert "coder" in text

    def test_empty_text_block_skipped(self):
        lines = render_message_blocks([self._msg([self._text_blk("")])], "coder")
        assert lines == []

    def test_whitespace_only_text_skipped(self):
        lines = render_message_blocks([self._msg([self._text_blk("   \n  ")])], "coder")
        assert lines == []

    def test_tool_use_block_renders_icon(self):
        lines = render_message_blocks([self._msg([self._tool_blk("read_file")])], "coder")
        assert len(lines) == 1
        icon_text = lines[0][1][1]  # frag [0]=leading space, [1]=icon frag
        assert "⚙" in icon_text

    def test_tool_use_block_shows_name(self):
        lines = render_message_blocks([self._msg([self._tool_blk("read_file")])], "coder")
        text = _flat_text(lines)
        assert "read_file" in text

    def test_tool_use_block_shows_args(self):
        lines = render_message_blocks(
            [self._msg([self._tool_blk("read_file", {"path": "/tmp/f.py"})])], "coder"
        )
        text = _flat_text(lines)
        assert "/tmp/f.py" in text

    def test_tool_use_icon_is_silver(self):
        lines = render_message_blocks([self._msg([self._tool_blk("read_file")])], "coder")
        icon_style = lines[0][1][0]  # frag [0]=leading space, [1]=icon frag → style
        assert "#C0C0C0" in icon_style

    def test_tool_use_no_args_no_key_value_frags(self):
        lines = render_message_blocks([self._msg([self._tool_blk("bash")])], "coder")
        row = lines[0]
        # no key=value frags when inputs is empty — only icon + name frags
        assert len(row) == 3  # leading space + icon + name

    def test_unknown_block_type_ignored(self):
        lines = render_message_blocks(
            [self._msg([{"type": "thinking", "text": "thinking…"}])], "coder"
        )
        assert lines == []


# ── user block messages (tool results) ───────────────────────────────────────

class TestUserBlockMessages:
    def _result_msg(self, content: str) -> dict:
        return {
            "role": "user",
            "type": "blocks",
            "blocks": [{"type": "tool_result", "content": content}],
        }

    def test_produces_three_rows(self):
        lines = render_message_blocks([self._result_msg("ok output")], "coder")
        assert len(lines) == 3  # ✓ done row + └─ preview row + blank

    def test_done_row_has_bold_green_style(self):
        lines = render_message_blocks([self._result_msg("ok")], "coder")
        done_style = _styles(lines)[0][0]
        assert "bold" in done_style
        assert "#4CAF50" in done_style

    def test_done_row_contains_done_text(self):
        lines = render_message_blocks([self._result_msg("ok")], "coder")
        assert "done" in _flat_text(lines)

    def test_preview_row_contains_content(self):
        lines = render_message_blocks([self._result_msg("my result")], "coder")
        assert "my result" in _flat_text(lines)

    def test_preview_row_has_branch(self):
        lines = render_message_blocks([self._result_msg("x")], "coder")
        assert "└─" in _flat_text(lines)

    def test_trailing_blank_row(self):
        lines = render_message_blocks([self._result_msg("x")], "coder")
        assert lines[-1] == [("", "")]

    def test_truncated_in_normal_mode(self):
        long = "z" * 200
        lines = render_message_blocks([self._result_msg(long)], "coder", expanded=False)
        text = _flat_text(lines)
        assert len(text) < 200

    def test_non_tool_result_blocks_ignored(self):
        msg = {
            "role": "user",
            "type": "blocks",
            "blocks": [{"type": "text", "text": "ignored"}],
        }
        lines = render_message_blocks([msg], "coder")
        assert lines == [[("", "")]]  # only the trailing blank


# ── multi-message sequence ────────────────────────────────────────────────────

class TestMultiMessageSequence:
    def test_full_turn_sequence(self):
        messages = [
            {"role": "user",      "type": "text",   "text": "Do X"},
            {"role": "assistant", "type": "blocks",  "blocks": [
                {"type": "text",     "text": "Sure"},
                {"type": "tool_use", "name": "bash", "input": {"command": "ls"}},
            ]},
            {"role": "user", "type": "blocks", "blocks": [
                {"type": "tool_result", "content": "file.py"},
            ]},
        ]
        lines = render_message_blocks(messages, "coder")
        flat = _flat_text(lines)
        assert "Do X" in flat
        assert "coder" in flat
        assert "Sure" in flat
        assert "bash" in flat
        assert "file.py" in flat

    def test_expanded_mode_propagates(self):
        messages = [
            {"role": "user", "type": "text", "text": "a" * 200},
        ]
        n = render_message_blocks(messages, "coder", expanded=False)
        e = render_message_blocks(messages, "coder", expanded=True)
        assert len(_flat_text([e[0]])) > len(_flat_text([n[0]]))
