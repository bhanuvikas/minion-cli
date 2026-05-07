"""Status bar for the TUI.

Composed of left_sections() and right_sections() — override in subclasses
to extend. Right content is padded to the terminal edge using the actual
terminal width read at render time.

Session info (model, provider, project, memory, agents) is set via
update_session() and rendered on the right side in minion palette colours:
  #FFD700  YELLOW  — thinking indicator
  #1E90FF  BLUE    — model name
  #4CAF50  GREEN   — memory enabled dot
  #C0C0C0  SILVER  — provider, separators
"""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText


Section = tuple[str, str]   # (pt_style, display_text)


class StatusBar:
    """Width-aware status bar built from composable section lists."""

    def __init__(self, model_name: str, width: int = 120) -> None:
        self._width    = width
        self._thinking = False

        # Session fields — set via update_session()
        self._model    = model_name
        self._provider = ""
        self._project  = ""
        self._cwd      = ""
        self._memory   = True
        self._agents   = 0
        self._version  = ""

    # ── State setters ─────────────────────────────────────────────────────────

    def set_thinking(self, thinking: bool) -> None:
        self._thinking = thinking

    def set_model(self, model_name: str) -> None:
        self._model = model_name

    def set_width(self, width: int) -> None:
        self._width = max(40, width)

    def update_session(
        self,
        *,
        model: str = "",
        provider: str = "",
        project: str = "",
        cwd: str = "",
        memory: bool = True,
        agents: int = 0,
        version: str = "",
    ) -> None:
        if model:
            self._model    = model
        self._provider = provider
        self._project  = project
        self._cwd      = cwd
        self._memory   = memory
        self._agents   = agents
        self._version  = version

    # ── Section builders ──────────────────────────────────────────────────────

    def left_sections(self) -> list[Section]:
        if self._thinking:
            return [("class:status-thinking", "● thinking…")]
        return []

    def right_sections(self) -> list[Section]:
        parts: list[Section] = []

        # Version
        if self._version:
            parts.append(("class:status-dim", f"v{self._version}"))

        # Memory dot — green when enabled, dim when not
        if self._memory:
            parts.append(("class:status-mem-on",  "● memory"))
        else:
            parts.append(("class:status-mem-off", "○ memory"))

        # Agent count (only if agents are loaded)
        if self._agents:
            label = f"{self._agents} agent{'s' if self._agents != 1 else ''}"
            parts.append(("class:status-dim", label))

        # cwd — show last two path components so context is clear
        if self._cwd:
            from pathlib import Path as _P
            p = _P(self._cwd)
            short = str(p).replace(str(_P.home()), "~")
            parts.append(("class:status-dim", short if len(short) <= 28 else "…/" + p.name))

        # Project name
        if self._project:
            proj = self._project if len(self._project) <= 16 else self._project[:14] + "…"
            parts.append(("class:status-project", proj))

        # Model name (always shown)
        model = self._model if len(self._model) <= 24 else self._model[:22] + "…"
        parts.append(("class:status-model", model))

        return parts

    # ── Render ────────────────────────────────────────────────────────────────

    def get_formatted_text(self) -> FormattedText:
        try:
            from prompt_toolkit.application.current import get_app
            width = get_app().output.get_size().columns
        except Exception:
            width = self._width

        left  = self.left_sections()
        right = self.right_sections()

        sep = ("class:status-dim", "  ·  ")

        # Build flat fragment list for left and right, joined by separator
        left_frags:  list[Section] = []
        for i, s in enumerate(left):
            left_frags.append(s)
            if i < len(left) - 1:
                left_frags.append(sep)

        right_frags: list[Section] = []
        for i, s in enumerate(right):
            right_frags.append(s)
            if i < len(right) - 1:
                right_frags.append(sep)

        left_len  = sum(len(t) for _, t in left_frags)
        right_len = sum(len(t) for _, t in right_frags)

        # 2-char left margin, 4-char right margin (avoids clipping at terminal edge)
        pad = max(1, width - 2 - left_len - right_len - 4)

        frags: list[Section] = [("class:status-bar", "  ")]
        frags.extend(left_frags)
        frags.append(("class:status-bar", " " * pad))
        frags.extend(right_frags)
        frags.append(("class:status-bar", "    "))

        return FormattedText(frags)
