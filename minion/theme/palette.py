"""Minion colour palette — Rich Theme and tool-name colour map.

Zero intra-package imports; safe to import from any other minion.theme submodule.
"""

from rich.theme import Theme

YELLOW = "#FFD700"
BLUE   = "#1E90FF"
GREEN  = "#4CAF50"
DENIM  = "#2F4F8F"
GREY   = "#888888"
SILVER = "#C0C0C0"

MINION_THEME = Theme(
    {
        "primary":   YELLOW,
        "secondary": BLUE,
        "accent":    DENIM,
        "muted":     GREY,
        "error":     "bold red",
        "success":   "bold green",
        "prompt":    f"bold {YELLOW}",
    }
)

_TOOL_NAME_COLORS: dict[str, str] = {
    "write_file": YELLOW,
    "edit_file":  YELLOW,
    "run_shell":  "red",
    "web_fetch":  "red",
}
