"""ConfirmationManager — single point for all dangerous-tool confirmation prompts."""

import asyncio
import threading


class ConfirmationManager:
    """Serialize tool confirmations and own the display pause/resume lifecycle.

    All confirmation prompts go through this class. Guarantees:
    - One prompt visible at a time (async lock for async path, sync lock for sync path)
    - AgentLiveDisplay paused before prompt, resumed after
    - Full scope selector (_interactive_confirm) for every confirmation, including MCP tools

    Use confirm_async() from the top-level event loop.
    Use confirm_sync() from threads (e.g. asyncio.to_thread workers, ThreadPoolExecutor).
    """

    def __init__(self, permission_store=None) -> None:
        self._permission_store = permission_store
        self._lock = threading.Lock()

    def confirm_sync(self, name: str, inputs: dict) -> bool:
        """Sync confirmation — serialized via threading.Lock.

        Safe to call from any context (threads, asyncio.to_thread workers, etc).
        Pauses any active AgentLiveDisplay so the prompt is fully visible.
        """
        from .tools.executor import _interactive_confirm
        from .agents.display import get_active_live_display
        with self._lock:
            display = get_active_live_display()
            if display is not None:
                display.pause()
            try:
                return _interactive_confirm(name, inputs, self._permission_store)
            finally:
                if display is not None:
                    display.resume()

    async def confirm_async(self, name: str, inputs: dict) -> bool:
        """Async confirmation — runs confirm_sync in a thread.

        Safe to call from any event loop. The single threading.Lock ensures
        prompts are fully serialized across all callers (async and threaded).
        """
        return await asyncio.to_thread(self.confirm_sync, name, inputs)
