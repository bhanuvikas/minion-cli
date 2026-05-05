"""HookRegistry — factory that builds a HookRunner from MinionConfig.

Open/Closed: adding HTTP or MCP handler types means adding to this factory only,
without modifying HookRunner or any injection site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .builtin.minion_md import MinionMdStalenessHandler
from .handler import HookHandler
from .handlers.shell import ShellHookHandler
from .runner import HookRunner

if TYPE_CHECKING:
    from ..config_file import MinionConfig


class HookRegistry:
    @staticmethod
    def from_config(config: "MinionConfig") -> HookRunner:
        """Build a HookRunner from config. Built-ins registered first,
        then user-defined shell handlers in config order."""
        handlers: list[HookHandler] = []

        if config.hooks_config.builtin_minion_md:
            handlers.append(MinionMdStalenessHandler())  # type: ignore[arg-type]

        for defn in config.hooks:
            handlers.append(ShellHookHandler(defn))  # type: ignore[arg-type]

        runner = HookRunner(handlers)
        if not config.hooks_config.enabled:
            runner.disable()
        return runner
