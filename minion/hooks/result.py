"""HookResult — returned by every hook handler execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class HookResult:
    action: Literal["proceed", "block"] = "proceed"
    reason: str = ""   # error message shown to user / fed to LLM on block
    tip: str = ""      # console tip shown after current turn completes
