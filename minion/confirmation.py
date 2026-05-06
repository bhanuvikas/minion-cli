"""ConfirmationManager — single point for all dangerous-tool confirmation prompts."""

import asyncio
import threading
from typing import Optional


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
        self._sync_lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None

    def _get_async_lock(self) -> asyncio.Lock:
        """Lazy-init so the lock belongs to the calling event loop."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def confirm_sync(self, name: str, inputs: dict) -> bool:
        """Sync confirmation — serialized via threading.Lock.

        Safe to call from threads (including asyncio.to_thread workers).
        Pauses any active AgentLiveDisplay so the prompt is fully visible.
        """
        from .tools.executor import _interactive_confirm
        from .agents.display import get_active_live_display
        with self._sync_lock:
            display = get_active_live_display()
            if display is not None:
                display.pause()
            try:
                return _interactive_confirm(name, inputs, self._permission_store)
            finally:
                if display is not None:
                    display.resume()

    async def confirm_async(self, name: str, inputs: dict) -> bool:
        """Async confirmation — serialized via asyncio.Lock.

        Use from the top-level event loop only (not from asyncio.to_thread workers,
        since asyncio.Lock is event-loop bound). Pauses any active display.
        """
        from .tools.executor import _interactive_confirm
        from .agents.display import get_active_live_display
        async with self._get_async_lock():
            display = get_active_live_display()
            if display is not None:
                display.pause()
            try:
                return await asyncio.to_thread(
                    _interactive_confirm, name, inputs, self._permission_store
                )
            finally:
                if display is not None:
                    display.resume()
