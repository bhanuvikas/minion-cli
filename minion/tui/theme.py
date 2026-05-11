"""TUI Textual theme — colour palette and CSS for MinionApp.

  #FFD700  GOLD    — user prefix, slot headers, permission cursor, thinking
  #1E90FF  BLUE    — minion prefix, model name in status bar
  #4CAF50  GREEN   — completed slots, success
  #C0C0C0  SILVER  — dim/muted text
  #666666  DIM     — very dim text
  #E8E8E8  TEXT    — primary readable text (input, body)
"""

# ── Palette (reused from minion/theme/palette.py) ─────────────────────────────

GOLD   = "#FFD700"
BLUE   = "#1E90FF"
GREEN  = "#4CAF50"
SILVER = "#C0C0C0"
DIM    = "#666666"
TEXT   = "#E8E8E8"

# ── Textual CSS (embedded in MinionApp.CSS) ───────────────────────────────────

MINION_TCSS = f"""
Screen {{
    layout: vertical;
    overflow: hidden hidden;
    background: #000000;
}}

ConversationArea {{
    height: 1fr;
    padding: 0 1;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {DIM};
}}

ConversationArea > Static {{
    height: auto;
    width: 1fr;
}}

SlotsZone {{
    height: auto;
    padding: 0 1;
    display: none;
}}

InspectorZone {{
    height: auto;
    display: none;
}}

InputSection {{
    height: auto;
    border-top: solid #2A2A2A;
}}

InputSection.permission-active {{
    border-top: solid {BLUE};
}}

PermissionContent {{
    display: none;
    height: auto;
    max-height: 35;
    overflow-y: auto;
    padding: 0 1;
}}

InputRow {{
    height: auto;
    padding: 0 1;
    layout: horizontal;
}}

.input-prefix {{
    width: auto;
    height: auto;
    padding: 0;
    color: {GOLD};
    text-style: bold;
}}

InputArea {{
    height: auto;
    min-height: 1;
    max-height: 6;
    border: none;
    background: transparent;
    padding: 0;
    color: {TEXT};
}}

InputArea:focus {{
    border: none;
    background: transparent;
}}

CompletionList {{
    display: none;
    height: auto;
    max-height: 10;
    background: #111111;
    border: solid {DIM};
    padding: 0;
}}

StatusLine {{
    dock: bottom;
    height: 1;
    background: transparent;
    color: {SILVER};
    padding: 0;
}}

Separator {{
    height: 1;
    background: {DIM};
    color: {DIM};
}}
"""
