"""TUI prompt_toolkit Style tokens — matches the minion palette in minion/theme.py.

  #FFD700  YELLOW  — user prefix, slot headers, permission cursor, thinking
  #1E90FF  BLUE    — minion prefix, model name in status bar
  #4CAF50  GREEN   — completed slots, success
  #C0C0C0  SILVER  — dim/muted text
"""

from prompt_toolkit.styles import Style

TUI_STYLE = Style.from_dict({
    # Conversation zone
    "you-prefix":       "bold #FFD700",
    "minion-prefix":    "bold #1E90FF",
    "system-prefix":    "#C0C0C0",
    "conv-text":        "",

    # Slots zone
    "slot-icon":        "bold #FFD700",
    "slot-label":       "bold",
    "slot-task":        "#C0C0C0",
    "slot-running":     "#C0C0C0",
    "slot-done":        "bold #4CAF50",
    "slot-error":       "bold red",
    "slot-detail":      "#C0C0C0",

    # Permission panel
    "perm-tool":        "bold #FFD700",
    "perm-detail":      "#C0C0C0",
    "perm-selected":    "bold #FFD700",
    "perm-option":      "",
    "perm-cursor":      "bold #FFD700",

    # Input prefix and inline syntax highlighting
    "input-prefix":     "bold #FFD700",
    "slash-command":    "bold #FFD700",
    "at-mention":       "bold #1E90FF",

    # Status bar — no background so it blends with the terminal background
    "status-bar":       "#C0C0C0",
    "status-dim":       "#666666",
    "status-model":     "#1E90FF",
    "status-project":   "#FFD700",
    "status-mem-on":    "#4CAF50",
    "status-mem-off":   "#666666",
    "status-thinking":  "bold #1E90FF",

    # Tool call zone (inline in conversation)
    "tool-pending":     "bold #FFD700",   # spinning frame while running
    "tool-icon":        "#C0C0C0",         # ⚙ when done
    "tool-name":        "bold",
    "tool-detail":      "#666666",         # key arg and summary
    "tool-ok":          "#4CAF50",         # ✓
    "tool-err":         "bold red",        # ✗

    # Thinking animation icon
    "thinking-icon":    "bold #FFD700",

    # Separator line
    "separator":        "#C0C0C0",
})
