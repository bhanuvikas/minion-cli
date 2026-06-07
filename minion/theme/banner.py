"""Figlet title, BANNER_COMMANDS, and greeting/startup-warning printers."""

from rich.text import Text

from .palette import BLUE, DENIM, GREEN, GREY, SILVER, YELLOW
from .console import console


# ─── Figlet Title ─────────────────────────────────────────────────────────────
# "MINION" rendered in figlet ASCII art with per-letter (and per-row) colors.
# Each entry in _LETTER_COLORS is either a uniform str or a list[str] of
# per-row colors (last entry repeats for any overflow rows).
#
# "big" font: 'i' has 6 rows — rows 0-1 are the dot, rows 2-5 are the stem.
# _I_DOT_ROWS is tied to _FIGLET_FONT; update both together if the font changes.

_FIGLET_FONT  = "big"  # upright block letters, no diagonal slant
_I_DOT_ROWS   = 2      # rows 0..1 of 'i' in "big" font are the tittle (dot)

_LETTER_COLORS: list[str | list[str]] = [
    YELLOW,                                              # m — uniform yellow
    [SILVER, SILVER, YELLOW, YELLOW, YELLOW, YELLOW],   # i — dot silver, stem yellow
    BLUE,                                                # n — uniform blue
    [SILVER, SILVER, YELLOW, YELLOW, YELLOW, YELLOW],   # i — dot silver, stem yellow
    YELLOW,                                              # o — uniform yellow
    BLUE,                                                # n — uniform blue
]


def _resolve_color(spec: str | list[str], row: int) -> str:
    """Return the color for a given row, given a uniform or per-row color spec."""
    if isinstance(spec, str):
        return spec
    return spec[row] if row < len(spec) else spec[-1]


def _build_title() -> Text:
    try:
        import pyfiglet
    except ImportError:
        # Graceful fallback: render plain text using each letter's body color
        t = Text(justify="center")
        for i, ch in enumerate("minion"):
            t.append(ch, style=_resolve_color(_LETTER_COLORS[i], _I_DOT_ROWS))
        t.append("\n")
        return t

    # Render each letter individually so we can apply per-letter/per-row colors.
    # pyfiglet renders monospace rows; combining row-by-row places letters
    # side-by-side naturally — no manual width calculation needed.
    letter_lines: list[list[str]] = []
    for letter in "minion":
        raw = pyfiglet.figlet_format(letter, font=_FIGLET_FONT)
        lines = raw.splitlines()
        # Strip trailing empty lines so all letters normalize to the same height
        while lines and not lines[-1].strip():
            lines.pop()
        letter_lines.append(lines)

    max_height = max(len(ls) for ls in letter_lines)

    # Pad any shorter letter to max_height with blank rows of matching width
    for ls in letter_lines:
        width = max((len(l) for l in ls), default=0)
        while len(ls) < max_height:
            ls.append(" " * width)

    title = Text(justify="center")
    for row in range(max_height):
        for ls, color_spec in zip(letter_lines, _LETTER_COLORS):
            title.append(ls[row], style=_resolve_color(color_spec, row))
        title.append("\n")

    return title


# ─── Banner ───────────────────────────────────────────────────────────────────

# Extend this list to add/reorder commands shown in the banner's left column.
BANNER_COMMANDS: list[tuple[str, str]] = [
    ("/help",    "show all commands"),
    ("/plan",    "create a step-by-step plan"),
    ("/compact", "summarise conversation"),
    ("/yolo",    "auto-approve all tools"),
    ("/model",   "switch provider or model"),
    ("/context", "show context window usage"),
    ("/clear",   "wipe conversation history"),
    ("/reflect", "enable self-critique mode"),
    ("/save",    "save current session"),
    ("/quit",    "exit Minion"),
]

_CMD_KEY_W = 10  # "/compact" (8) + 2 trailing spaces
_SEP_W     = 3   # fixed width of the │ separator column


def _build_session_rows(
    version: str,
    model: str,
    provider: str,
    project_name: str,
    a2a_count: int,
    cwd: str,
    agent_count: int,
    memory_enabled: bool,
    mcp_count: int,
    max_val: int,
) -> list[tuple[str, str, str]]:
    """Return (key, value, style) rows for the session column.

    Add new session facts here — each tuple renders as one row in the right column.
    """
    from pathlib import Path as _Path

    def _sv(s: str) -> str:
        return s if len(s) <= max_val else s[:max_val - 1] + "…"

    rows: list[tuple[str, str, str]] = []
    rows.append(("version", _sv(f"v{version}"), "white"))
    if model:
        rows.append(("model", _sv(model), BLUE))
    if project_name:
        rows.append(("project", _sv(project_name), "white"))
    if a2a_count > 0:
        lbl = "1 remote agent" if a2a_count == 1 else f"{a2a_count} remote agents"
        rows.append(("a2a", _sv(lbl), "white"))
    if cwd:
        cwd_display = cwd
        home = str(_Path.home())
        if cwd_display.startswith(home):
            cwd_display = "~" + cwd_display[len(home):]
        if len(cwd_display) > max_val:
            cwd_display = "…" + cwd_display[-(max_val - 1):]
        rows.append(("cwd", cwd_display, "white"))
    if agent_count > 0:
        lbl = "1 role loaded" if agent_count == 1 else f"{agent_count} roles loaded"
        rows.append(("agents", _sv(lbl), "white"))
    mem_val   = "enabled" if memory_enabled else "disabled"
    mem_style = GREEN     if memory_enabled else f"dim {GREY}"
    rows.append(("memory", mem_val, mem_style))
    if mcp_count > 0:
        lbl = "1 server active" if mcp_count == 1 else f"{mcp_count} servers active"
        rows.append(("mcp", _sv(lbl), "white"))
    return rows


def _build_greeting_text() -> Text:
    t = Text()
    t.append("Bello! ", style=f"bold {YELLOW}")
    t.append("I'm ", style="white")
    t.append("Minion", style=f"bold {BLUE}")
    t.append(". What do you want me to do?", style="white")
    return t


def _build_info_panel_renderable(session_rows: list[tuple[str, str, str]]):
    """Return a Table renderable for the info panel (auto-expands to container width)."""
    from rich.table import Table

    dots = ". " * 40  # long enough to fill any column width; no_wrap truncates cleanly

    cmd_text = Text()
    cmd_text.append(f"{'command':<{_CMD_KEY_W}}", style=f"bold {YELLOW}")
    cmd_text.append("description\n", style=GREY)
    cmd_text.append(dots + "\n", style=f"dim {GREY}")
    for i, (cmd, desc) in enumerate(BANNER_COMMANDS):
        cmd_text.append(f"{cmd:<{_CMD_KEY_W}}", style=f"bold {YELLOW}")
        suffix = "\n" if i < len(BANNER_COMMANDS) - 1 else ""
        cmd_text.append(desc + suffix, style="white")

    sess_text = Text()
    sess_text.append("  session\n", style=f"bold {YELLOW}")
    sess_text.append("  " + dots + "\n", style=f"dim {GREY}")
    for i, (key, val, val_style) in enumerate(session_rows):
        sess_text.append(f"  {key:<9}", style=GREY)
        suffix = "\n" if i < len(session_rows) - 1 else ""
        sess_text.append(val + suffix, style=val_style)

    n_sep = max(2 + len(BANNER_COMMANDS), 2 + len(session_rows))
    sep_text = Text("\n".join(["│"] * n_sep), style=f"dim {SILVER}", justify="center")

    outer = Table.grid(expand=True)
    outer.add_column(ratio=50, no_wrap=True)
    outer.add_column(width=_SEP_W, justify="center")
    outer.add_column(ratio=50, no_wrap=True)
    outer.add_row(cmd_text, sep_text, sess_text)
    return outer


def get_greeting_renderables(
    version: str = "",
    model: str = "",
    provider: str = "",
    project_name: str = "",
    cwd: str = "",
    agent_count: int = 0,
    memory_enabled: bool = True,
    mcp_count: int = 0,
    a2a_count: int = 0,
) -> list:
    """Return Rich renderables for the greeting banner.

    Consumed by both console mode (via print_greeting) and TUI mode (via
    _write_banner) — single source of truth for banner layout.
    """
    from rich.align import Align
    from rich.rule import Rule
    from .. import __version__

    art = _build_title()
    art.justify = None

    # Compute max_val from terminal width — same formula used for both modes.
    term_w  = console.size.width
    left_w  = max(30, (term_w - _SEP_W) // 2)
    right_w = max(20, term_w - _SEP_W - left_w)
    max_val = max(8, right_w - 11 - 1)

    session_rows = _build_session_rows(
        version=version or __version__,
        model=model,
        provider=provider,
        project_name=project_name,
        a2a_count=a2a_count,
        cwd=cwd,
        agent_count=agent_count,
        memory_enabled=memory_enabled,
        mcp_count=mcp_count,
        max_val=max_val,
    )

    def _blank() -> Text:
        return Text(" ")

    return [
        _blank(),
        Align(art, align="center"),
        Align(_build_greeting_text(), align="center"),
        _blank(),
        Rule(style=SILVER),
        _build_info_panel_renderable(session_rows),
        _blank(),
        Rule(style=SILVER),
        _blank(),
    ]


def get_startup_warning_renderables(warnings: list[str]) -> list:
    """Return Rich renderables for startup warnings.

    Consumed by both console mode (via print_startup_warnings) and TUI mode
    (via _write_banner) — single source of truth for warning layout.
    Returns an empty list when there are no warnings.
    """
    if not warnings:
        return []
    from rich.rule import Rule
    items: list = [Text.from_markup(w) for w in warnings]
    items.append(Text(" "))
    items.append(Rule(style=SILVER))
    return items


def print_greeting(
    version: str = "",
    model: str = "",
    provider: str = "",
    project_name: str = "",
    cwd: str = "",
    agent_count: int = 0,
    memory_enabled: bool = True,
    mcp_count: int = 0,
    a2a_count: int = 0,
) -> None:
    for r in get_greeting_renderables(
        version=version,
        model=model,
        provider=provider,
        project_name=project_name,
        cwd=cwd,
        agent_count=agent_count,
        memory_enabled=memory_enabled,
        mcp_count=mcp_count,
        a2a_count=a2a_count,
    ):
        console.print(r)


def print_startup_warnings(warnings: list[str]) -> None:
    """Print startup warnings collected by loaders, followed by a closing rule.

    No-op when warnings is empty. Each entry is a Rich markup string.
    Add new warning sources by appending to the startup_warnings list before
    print_greeting() is called, or pass extra warnings directly to this function.
    """
    for r in get_startup_warning_renderables(warnings):
        console.print(r)
