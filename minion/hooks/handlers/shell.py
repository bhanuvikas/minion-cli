"""ShellHookHandler — executes user-defined shell commands at hook fire points.

Stdin: JSON-encoded event payload.
Stdout: JSON with optional "tip" and "reason" fields (parsed on exit 0).
Exit codes:
  0  — proceed; stdout JSON parsed for tip/reason
  2  — block (PreToolUse by default, or when blocking=True); stderr used as reason
  other — non-blocking; execution continues
"""

from __future__ import annotations

import asyncio
import json

from ..events import HookEvent, PreToolUseEvent
from ..result import HookResult


class ShellHookHandler:
    def __init__(self, definition: "HookDefinition") -> None:  # type: ignore[name-defined]
        self._defn = definition

    def matches(self, event: HookEvent) -> bool:
        if event.event_name != self._defn.event:
            return False
        if self._defn.tool is not None:
            tool_name = getattr(event, "tool_name", None)
            if tool_name != self._defn.tool:
                return False
        return True

    async def execute(self, event: HookEvent) -> HookResult:
        stdin_bytes = json.dumps(event.to_json_dict()).encode()
        try:
            proc = await asyncio.create_subprocess_shell(
                self._defn.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(event.cwd),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_bytes),
                timeout=float(self._defn.timeout),
            )
        except asyncio.TimeoutError:
            return HookResult(tip=f"[hook '{self._defn.command}' timed out after {self._defn.timeout}s]")
        except Exception as e:
            return HookResult(tip=f"[hook error: {e}]")

        if proc.returncode == 2:
            # Blocking exit — PreToolUse blocks by default; override with blocking=True/False
            is_blocking = self._defn.blocking if self._defn.blocking is not None else (
                isinstance(event, PreToolUseEvent)
            )
            if is_blocking:
                return HookResult(action="block", reason=stderr.decode().strip() or f"Hook blocked {getattr(event, 'tool_name', 'action')}.")

        if proc.returncode == 0 and stdout.strip():
            try:
                data = json.loads(stdout.decode())
                return HookResult(
                    tip=data.get("tip", ""),
                    reason=data.get("reason", ""),
                )
            except json.JSONDecodeError:
                pass

        return HookResult()
