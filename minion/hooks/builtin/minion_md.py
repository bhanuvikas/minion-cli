"""MinionMdStalenessHandler — built-in hook that tips the user when MINION.md
may be stale because a source file was written or edited during the turn.
"""

from __future__ import annotations

from pathlib import Path

from ..events import HookEvent, PostToolUseEvent
from ..result import HookResult


_SOURCE_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx",
    ".go", ".rs", ".java", ".rb", ".cs",
    ".yaml", ".yml", ".toml", ".json",
    ".sh", ".bash", ".zsh", ".md",
})


class MinionMdStalenessHandler:
    """Fires on PostToolUse for write_file / edit_file when MINION.md exists
    in cwd and a source file was changed. Returns a tip that the REPL prints
    after the turn so users know to re-run /init.
    """

    def matches(self, event: HookEvent) -> bool:
        if not isinstance(event, PostToolUseEvent):
            return False
        if event.tool_name not in ("write_file", "edit_file"):
            return False
        if not (event.cwd / "MINION.md").exists():
            return False
        path = Path(event.tool_input.get("path", ""))
        return (
            path.suffix in _SOURCE_EXTENSIONS
            and path.name != "MINION.md"
        )

    async def execute(self, event: PostToolUseEvent) -> HookResult:  # type: ignore[override]
        return HookResult(
            tip="MINION.md may be stale — source files were modified. Run /init to refresh."
        )

    def hook_describe(self) -> dict:
        return {
            "name": "minion-md-staleness",
            "source": "builtin",
            "type": "python",
            "event": "PostToolUse",
            "tool": "write_file, edit_file",
            "detail": "Tips when source files change while MINION.md exists",
        }
