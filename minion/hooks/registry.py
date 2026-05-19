"""HookRegistry — 3-tier YAML loader for hook manifests.

Tiers (lowest to highest priority — last write wins on name collision):
  1. builtin  — Python handlers registered directly (MinionMdStalenessHandler, etc.)
  2. user     — ~/.minion/hooks/*.yaml
  3. project  — <cwd>/.minion/hooks/*.yaml

A project hook with the same name as a user hook shadows the user hook.
Builtin Python handlers are always registered first via build_runner().
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ItemsView, Iterator, Optional

import yaml

from ..theme import startup_warnings
from .manifest import HookManifest, load_manifest
from .runner import HookRunner

if TYPE_CHECKING:
    from ..config.file import MinionConfig

_USER_DIR = Path.home() / ".minion" / "hooks"


class HookRegistry:
    """Immutable mapping of hook name → HookManifest plus builtin config.

    Created by HookRegistry.load(). Provides manifest access for browsing
    (future /hooks TUI modal) and produces a HookRunner via build_runner().
    """

    def __init__(self, manifests: dict[str, HookManifest], config: "MinionConfig") -> None:
        self._manifests = manifests
        self._config = config

    @classmethod
    def load(cls, cwd: Path, config: "MinionConfig") -> "HookRegistry":
        """Build registry from user + project YAML tiers.

        Project hooks shadow user hooks on name collision.
        Invalid or unreadable files are skipped with a startup warning.
        """
        manifests: dict[str, HookManifest] = {}
        tiers = [
            (_USER_DIR, "user"),
            (cwd / ".minion" / "hooks", "project"),
        ]
        for tier_dir, source in tiers:
            if not tier_dir.is_dir():
                continue
            for yaml_path in sorted(tier_dir.glob("*.yaml")):
                try:
                    manifest = load_manifest(yaml_path, source=source)
                    manifests[manifest.name] = manifest
                except (ValueError, yaml.YAMLError, OSError) as exc:
                    startup_warnings.append(
                        f"  [bold #a8a8a8]Warning[/]  "
                        f"[#888888]skipping hook '{yaml_path.name}': {exc}[/]"
                    )
        return cls(manifests, config)

    # ── Manifest access ────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[HookManifest]:
        return self._manifests.get(name)

    def items(self) -> ItemsView[str, HookManifest]:
        return self._manifests.items()

    def __iter__(self) -> Iterator[str]:
        return iter(self._manifests)

    def __len__(self) -> int:
        return len(self._manifests)

    def __contains__(self, name: str) -> bool:
        return name in self._manifests

    # ── Runner factory ─────────────────────────────────────────────────────────

    def build_runner(self) -> HookRunner:
        """Build a HookRunner: builtin Python handlers first, then YAML shell hooks."""
        from .builtin.minion_md import MinionMdStalenessHandler
        from .handlers.shell import ShellHookHandler
        from .handler import HookHandler

        handlers: list[HookHandler] = []

        if self._config.hooks_config.builtin_minion_md:
            handlers.append(MinionMdStalenessHandler())  # type: ignore[arg-type]

        for manifest in self._manifests.values():
            handlers.append(ShellHookHandler(manifest))  # type: ignore[arg-type]

        runner = HookRunner(handlers)
        if not self._config.hooks_config.enabled:
            runner.disable()
        return runner
