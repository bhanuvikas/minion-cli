"""ConfirmationManager — single point for all dangerous-tool confirmation prompts."""

import asyncio
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..tui.app import MinionApp


class ConfirmationManager:
    """Serialize tool confirmations and own the display pause/resume lifecycle.

    All confirmation prompts go through this class. Guarantees:
    - One prompt visible at a time (threading.Lock serializes all callers)
    - ParallelDisplay paused before prompt, resumed after (non-TUI path)
    - Full scope selector for every confirmation, including MCP tools

    In TUI mode (set_tui() called):
    - Routes to PermissionPanel.request() in the TUI event loop
    - No display pause/resume needed (TUI Application owns the terminal)

    Use confirm_async() from the top-level event loop.
    Use confirm_sync() from threads (e.g. asyncio.to_thread workers, ThreadPoolExecutor).
    """

    def __init__(self, permission_store=None) -> None:
        self._permission_store = permission_store
        self._lock = threading.Lock()
        self._tui_app: Optional["MinionApp"] = None
        self._tui_loop: Optional[asyncio.AbstractEventLoop] = None

    def set_tui(
        self,
        tui_app: "MinionApp",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Wire a MinionApp into this manager for TUI-mode confirmations.

        Call from the TUI event loop so the stored loop reference is correct.
        After this, confirm_sync() routes to the TUI permission panel instead
        of questionary.
        """
        self._tui_app  = tui_app
        self._tui_loop = loop

    def confirm_sync(self, name: str, inputs: dict, diff_lines: str = "") -> bool:
        """Sync confirmation — serialized via threading.Lock.

        TUI path: schedules PermissionPanel.request() in the TUI event loop
        and blocks the calling thread until the user responds.

        Non-TUI path: pauses any active AgentLiveDisplay, shows the full
        _interactive_confirm scope selector via questionary, then resumes.

        Safe to call from any context (threads, asyncio.to_thread workers, etc).
        """
        with self._lock:
            # ── TUI path ──────────────────────────────────────────────────────
            if self._tui_app is not None and self._tui_loop is not None:
                future = asyncio.run_coroutine_threadsafe(
                    self._tui_app.permission.request(name, inputs, diff_lines=diff_lines),
                    self._tui_loop,
                )
                return future.result()

            # ── Non-TUI path ─────────────────────────────────────────────────
            from .executor import _interactive_confirm
            from ..agents.display import get_active_live_display
            display = get_active_live_display()
            if display is not None:
                display.pause()
            try:
                return _interactive_confirm(name, inputs, self._permission_store)
            finally:
                if display is not None:
                    display.resume()

    async def confirm_async(self, name: str, inputs: dict, diff_lines: str = "") -> bool:
        """Async confirmation — runs confirm_sync in a thread.

        Safe to call from any event loop. The single threading.Lock ensures
        prompts are fully serialized across all callers (async and threaded).
        """
        return await asyncio.to_thread(self.confirm_sync, name, inputs, diff_lines)

    def choose_sync(self, prompt: str, choices: list[str]) -> "Optional[int]":
        """Present a numbered choice list. Returns chosen index or None (cancel).

        TUI path: shows ChoicePanel inline (replaces InputRow until user picks).
        Console path: uses questionary.select().
        """
        from typing import Optional as _Opt
        with self._lock:
            if self._tui_app is not None and self._tui_loop is not None:
                future = asyncio.run_coroutine_threadsafe(
                    self._tui_app.choice_panel.request(prompt, choices),
                    self._tui_loop,
                )
                return future.result()
            # Console path
            import questionary
            from ..config.interactive import MINION_STYLE
            result = questionary.select(
                prompt,
                choices=choices,
                pointer="  ❯ ",
                style=MINION_STYLE,
            ).ask()
            if result in choices:
                return choices.index(result)
            return None

    def text_input_sync(self, prompt: str) -> "Optional[str]":
        """Capture a single line of text input. Returns the text or None (cancel).

        TUI path: switches InputRow to capture mode; next Enter goes to capture.
        Console path: uses console.input().
        """
        with self._lock:
            if self._tui_app is not None and self._tui_loop is not None:
                from ..tui.app import _TextCapture
                capture = _TextCapture()
                asyncio.run_coroutine_threadsafe(
                    self._tui_app.start_text_capture(prompt, capture),
                    self._tui_loop,
                ).result()
                capture.event.wait()
                return capture.result or None
            # Console path
            from ..theme import console
            from ..theme import YELLOW
            try:
                result = console.input(f"[bold {YELLOW}]{prompt}[/] › ")
                return result.strip() or None
            except (KeyboardInterrupt, EOFError):
                return None
