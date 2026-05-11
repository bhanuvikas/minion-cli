"""Tests for minion.tui.terminal — environment-based terminal detection."""

import pytest
from unittest.mock import patch


def _detect(env: dict) -> str:
    from minion.tui.terminal import detect_terminal
    with patch.dict("os.environ", env, clear=True):
        return detect_terminal()


def _tip(env: dict) -> str:
    from minion.tui.terminal import get_selection_tip
    with patch.dict("os.environ", env, clear=True):
        return get_selection_tip()


class TestDetectTerminal:
    def test_kitty_via_window_id(self):
        assert _detect({"KITTY_WINDOW_ID": "1"}) == "kitty"

    def test_kitty_via_term(self):
        assert _detect({"TERM": "xterm-kitty"}) == "kitty"

    def test_kitty_window_id_takes_priority_over_term_program(self):
        # KITTY_WINDOW_ID wins even if TERM_PROGRAM says something else
        assert _detect({"KITTY_WINDOW_ID": "2", "TERM_PROGRAM": "Apple_Terminal"}) == "kitty"

    def test_wezterm_via_pane(self):
        assert _detect({"WEZTERM_PANE": "0"}) == "wezterm"

    def test_wezterm_via_unix_socket(self):
        assert _detect({"WEZTERM_UNIX_SOCKET": "/tmp/wez"}) == "wezterm"

    def test_iterm2(self):
        assert _detect({"TERM_PROGRAM": "iTerm.app"}) == "iterm2"

    def test_apple_terminal(self):
        assert _detect({"TERM_PROGRAM": "Apple_Terminal"}) == "terminal_app"

    def test_vscode(self):
        assert _detect({"TERM_PROGRAM": "vscode"}) == "vscode"

    def test_hyper(self):
        assert _detect({"TERM_PROGRAM": "Hyper"}) == "hyper"

    def test_ghostty(self):
        assert _detect({"TERM_PROGRAM": "ghostty"}) == "ghostty"

    def test_tmux(self):
        assert _detect({"TMUX": "/tmp/tmux-123/default,12,0"}) == "tmux"

    def test_screen(self):
        assert _detect({"STY": "12345.pts-0.host"}) == "screen"

    def test_ssh_via_client(self):
        assert _detect({"SSH_CLIENT": "1.2.3.4 55000 22"}) == "ssh"

    def test_ssh_via_tty(self):
        assert _detect({"SSH_TTY": "/dev/pts/0"}) == "ssh"

    def test_unknown(self):
        assert _detect({}) == "unknown"

    def test_wezterm_before_term_program(self):
        # WEZTERM_PANE should win over TERM_PROGRAM
        assert _detect({"WEZTERM_PANE": "0", "TERM_PROGRAM": "iTerm.app"}) == "wezterm"


class TestGetSelectionTip:
    def test_iterm2_includes_option(self):
        tip = _tip({"TERM_PROGRAM": "iTerm.app"})
        assert "Option" in tip
        assert "ctrl+y" in tip

    def test_kitty_includes_shift(self):
        tip = _tip({"KITTY_WINDOW_ID": "1"})
        assert "Shift" in tip
        assert "ctrl+y" in tip

    def test_wezterm_includes_alt(self):
        tip = _tip({"WEZTERM_PANE": "0"})
        assert "Alt" in tip
        assert "ctrl+y" in tip

    def test_ssh_only_ctrl_y(self):
        tip = _tip({"SSH_CLIENT": "1.2.3.4 55000 22"})
        assert "ctrl+y" in tip
        # No drag tip for SSH
        assert "dragging" not in tip

    def test_unknown_includes_generic_hint(self):
        tip = _tip({})
        assert "ctrl+y" in tip
        assert "Shift" in tip or "Option" in tip or "Alt" in tip

    def test_returns_rich_markup_string(self):
        tip = _tip({"TERM_PROGRAM": "iTerm.app"})
        assert isinstance(tip, str)
        assert "[" in tip  # contains Rich markup

    def test_no_tip_label_prefix(self):
        # get_selection_tip() returns just the message; caller adds "Tip" prefix
        tip = _tip({"TERM_PROGRAM": "iTerm.app"})
        assert not tip.startswith("[dim]")
        assert not tip.lower().startswith("tip:")
