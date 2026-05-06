"""HookResult — returned by every hook handler execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class HookResult:
    action: Literal["proceed", "block"] = "proceed"
    reason: str = ""           # error message shown to user / fed to LLM on block
    tip: str = ""              # console tip shown after current turn completes
    exit_code: Optional[int] = None  # shell exit code; None for built-in handlers
