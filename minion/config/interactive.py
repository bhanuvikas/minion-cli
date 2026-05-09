"""Interactive configuration and .env management for minion-cli.

Handles reading/writing the .env file and the /model interactive flow
that lets users switch provider, model, and API keys without editing
files manually.
"""

import os
import shutil
from pathlib import Path
from typing import Optional

import questionary
from questionary import Style

from ..llm.base import LLMClient
from ..theme import BLUE, YELLOW, console, print_model_info

# ─── Questionary style that matches Minion theme ─────────────────────────────
MINION_STYLE = Style(
    [
        ("qmark", f"fg:{YELLOW} bold"),
        ("question", "bold"),
        ("answer", f"fg:{BLUE} bold"),
        ("pointer", f"fg:{YELLOW} bold"),
        ("highlighted", f"fg:{YELLOW} bold"),
        ("selected", f"fg:{BLUE}"),
        ("instruction", "fg:#888888"),
    ]
)

# Map provider name → env var for its API key
PROVIDER_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

PROVIDERS = list(PROVIDER_KEY_MAP.keys())


# ─── .env file read / write ───────────────────────────────────────────────────

def _find_env_file() -> Path:
    """Return the path to the .env file, creating it from .env.example if absent."""
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        if example.exists():
            shutil.copy(example, env_path)
            console.print("[muted].env created from .env.example[/]")
        else:
            env_path.touch()
    return env_path


def update_env_values(updates: dict) -> None:
    """Update specific keys in the .env file, preserving all comments and other keys.

    Keys present in `updates` are overwritten in-place. Keys not yet in the
    file are appended at the end. Everything else (comments, blank lines,
    unrelated keys) is left exactly as-is.
    """
    env_path = _find_env_file()

    lines = env_path.read_text().splitlines(keepends=True)
    updated_keys: set = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()
        # Skip comments and blanks
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Append any keys that weren't already in the file
    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}\n")

    env_path.write_text("".join(new_lines))


# ─── Interactive /model config flow ──────────────────────────────────────────

def run_model_config(client: LLMClient) -> Optional[dict]:
    """Interactive provider + model selection via questionary.

    Shows the current provider highlighted in a dropdown, then a text field
    for the model ID. If the provider changes, offers to update the API key.
    Any confirmed changes are written back to .env and returned as a dict
    so the caller can reload the client.

    Returns a dict of {setting: new_value} for changed settings, or None
    if the user cancelled or made no changes.
    """
    console.print(f"\n[bold {YELLOW}]Configure Model[/]\n")

    try:
        # ── Provider selection ────────────────────────────────────────────────
        provider = questionary.select(
            " Provider:",
            choices=PROVIDERS,
            default=client.provider_name,
            pointer="  ❯ ",
            style=MINION_STYLE,
        ).ask()

        if provider is None:  # user hit Ctrl+C
            console.print("[muted]Cancelled.[/]")
            return None

        updates: dict = {}

        # ── API key update (only when provider changed) ───────────────────────
        if provider != client.provider_name:
            key_var = PROVIDER_KEY_MAP[provider]
            current_key = os.getenv(key_var, "")
            masked = f"...{current_key[-6:]}" if len(current_key) > 6 else "(not set)"

            update_key = questionary.confirm(
                f" Update {key_var}? (current: {masked})",
                default=not bool(current_key),
                style=MINION_STYLE,
            ).ask()

            if update_key is None:
                console.print("[muted]Cancelled.[/]")
                return None

            if update_key:
                api_key = questionary.password(
                    f" {key_var}:",
                    style=MINION_STYLE,
                ).ask()
                if api_key is None:
                    console.print("[muted]Cancelled.[/]")
                    return None
                if api_key.strip():
                    updates[key_var] = api_key.strip()

            updates["MINION_PROVIDER"] = provider

        # ── Model ID ─────────────────────────────────────────────────────────
        model = questionary.text(
            " Model ID:",
            default=client.model_id,
            style=MINION_STYLE,
        ).ask()

        if model is None:
            console.print("[muted]Cancelled.[/]")
            return None

        model = model.strip()
        if model and model != client.model_id:
            updates["MINION_MODEL"] = model

        if not updates:
            console.print("[muted]No changes.[/]")
            return None

        # ── Confirm ───────────────────────────────────────────────────────────
        console.print()
        for k, v in updates.items():
            masked_v = f"...{v[-6:]}" if "KEY" in k and len(v) > 6 else v
            console.print(f"  [secondary]{k}[/] = {masked_v}")
        console.print()

        confirmed = questionary.confirm(
            " Save these changes to .env?",
            default=True,
            style=MINION_STYLE,
        ).ask()

        if confirmed:
            update_env_values(updates)
            console.print(f"[success]✓[/]  .env updated. Restart minion to apply changes.")
            return updates

        console.print("[muted]Discarded.[/]")
        return None

    except (KeyboardInterrupt, EOFError):
        console.print("\n[muted]Cancelled.[/]")
        return None
