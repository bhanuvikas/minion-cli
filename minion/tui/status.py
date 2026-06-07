"""Status bar for the TUI.

Session info (model, provider, project, memory, agents) is set via
update_session() and rendered as a Rich markup string for the StatusLine
Static widget.

  #FFD700  GOLD   — project name
  #1E90FF  BLUE   — model name
  #4CAF50  GREEN  — memory enabled dot
  #C0C0C0  SILVER — dim/muted text
"""

from __future__ import annotations


class StatusBar:
    """Width-aware status bar — renders as a Rich markup string."""

    def __init__(self, model_name: str, width: int = 120) -> None:
        self._width    = width
        self._thinking = False
        self._inspector_hint = ""

        self._model     = model_name
        self._provider  = ""
        self._project   = ""
        self._cwd       = ""
        self._memory    = True
        self._agents    = 0
        self._version   = ""
        self._mcp_count = 0
        self._a2a_count = 0

    # ── State setters ─────────────────────────────────────────────────────────

    def set_thinking(self, thinking: bool) -> None:
        self._thinking = thinking

    def set_inspector_hint(self, hint: str) -> None:
        self._inspector_hint = hint

    def set_model(self, model_name: str) -> None:
        self._model = model_name

    def set_width(self, width: int) -> None:
        self._width = max(40, width)

    def update_session(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        project: str | None = None,
        cwd: str | None = None,
        memory: bool | None = None,
        agents: int | None = None,
        version: str | None = None,
        mcp_count: int | None = None,
        a2a_count: int | None = None,
    ) -> None:
        if model is not None:
            self._model     = model
        if provider is not None:
            self._provider  = provider
        if project is not None:
            self._project   = project
        if cwd is not None:
            self._cwd       = cwd
        if memory is not None:
            self._memory    = memory
        if agents is not None:
            self._agents    = agents
        if version is not None:
            self._version   = version
        if mcp_count is not None:
            self._mcp_count = mcp_count
        if a2a_count is not None:
            self._a2a_count = a2a_count

    # ── Section builders ──────────────────────────────────────────────────────

    def _left_parts(self) -> list[tuple[str, str]]:
        """Return (rich_style, text) pairs for left section."""
        if self._inspector_hint:
            return [("#666666", self._inspector_hint)]
        from .agent_registry import get_registry as _gr
        if len(_gr()):
            return [("#666666", "ctrl+o to inspect agents")]
        return []

    def _right_parts(self) -> list[tuple[str, str]]:
        """Return (rich_style, text) pairs for right section."""
        parts: list[tuple[str, str]] = []
        if self._version:
            parts.append(("#666666", f"v{self._version}"))
        if self._memory:
            parts.append(("#4CAF50", "● memory"))
        else:
            parts.append(("#666666", "○ memory"))
        if self._agents:
            label = f"{self._agents} agent{'s' if self._agents != 1 else ''}"
            parts.append(("#666666", label))
        if self._cwd:
            from pathlib import Path as _P
            p = _P(self._cwd)
            short = str(p).replace(str(_P.home()), "~")
            parts.append(("#666666", short if len(short) <= 28 else "…/" + p.name))
        if self._project:
            proj = self._project if len(self._project) <= 16 else self._project[:14] + "…"
            parts.append(("#FFD700", proj))
        model = self._model if len(self._model) <= 24 else self._model[:22] + "…"
        parts.append(("#1E90FF", model))
        return parts

    # ── Render ────────────────────────────────────────────────────────────────

    def get_rich_markup(self) -> str:
        """Return a Rich markup string for StatusLine.update()."""
        width = self._width

        left  = self._left_parts()
        right = self._right_parts()

        sep = ("[#666666]  ·  [/]",)

        def _join(parts: list[tuple[str, str]]) -> str:
            out: list[str] = []
            for i, (style, text) in enumerate(parts):
                out.append(f"[{style}]{text}[/]")
                if i < len(parts) - 1:
                    out.append("[#666666]  ·  [/]")
            return "".join(out)

        left_str  = _join(left)
        right_str = _join(right)

        # Compute plain lengths for padding
        left_len  = sum(len(t) for _, t in left)
        right_len = sum(len(t) for _, t in right)
        sep_len   = (len(left) - 1) * 5 if len(left) > 1 else 0
        sep_r_len = (len(right) - 1) * 5 if len(right) > 1 else 0
        pad = max(1, width - 2 - left_len - sep_len - right_len - sep_r_len - 4)

        return "  " + left_str + " " * pad + right_str + "    "
