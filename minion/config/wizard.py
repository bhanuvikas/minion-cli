"""First-run interactive setup wizard.

Detects missing API credentials and guides the user through:
  1. Provider selection (Anthropic / OpenAI / OpenRouter)
  2. API key entry
  3. Writing the key to a .env file in the current directory

Designed to run once on first launch and to be re-runnable via `minion setup`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import questionary

from ..theme import BLUE, SILVER, YELLOW, console

# Questionary style matching the rest of the codebase
from questionary import Style as _QStyle

_MINION_STYLE = _QStyle([
    ("qmark",        f"fg:{YELLOW} bold"),
    ("question",     "bold"),
    ("answer",       f"fg:{BLUE} bold"),
    ("pointer",      f"fg:{YELLOW} bold"),
    ("highlighted",  f"fg:{BLUE} bold"),
    ("selected",     f"fg:{BLUE}"),
    ("separator",    f"fg:{SILVER}"),
    ("instruction",  "fg:#888888"),
])

_PROVIDERS = {
    "Anthropic (Claude)":    ("ANTHROPIC_API_KEY",   "https://console.anthropic.com/settings/keys"),
    "OpenAI (GPT)":          ("OPENAI_API_KEY",      "https://platform.openai.com/api-keys"),
    "OpenRouter":            ("OPENROUTER_API_KEY",  "https://openrouter.ai/keys"),
}


async def run_setup_wizard() -> bool:
    """Run the interactive first-run setup wizard.

    Returns True if setup completed successfully, False if the user cancelled.
    Writes the chosen API key to .env in the current working directory.
    """
    console.print()
    console.print(f"  [bold {YELLOW}]Welcome to minion-cli![/]")
    console.print(f"  [dim {SILVER}]No API key found. Let's get you set up in 30 seconds.[/]")
    console.print()

    try:
        # Step 1 — provider selection
        provider_choice = await asyncio.to_thread(
            questionary.select(
                "Which LLM provider would you like to use?",
                choices=list(_PROVIDERS.keys()),
                style=_MINION_STYLE,
                pointer="  ❯",
            ).ask
        )
        if provider_choice is None:
            console.print(f"\n  [dim {SILVER}]Setup cancelled. Run [bold]minion setup[/] to configure later.[/]\n")
            return False

        env_var, docs_url = _PROVIDERS[provider_choice]

        # Step 2 — API key entry
        console.print(f"\n  [dim]Get your key at: {docs_url}[/]")
        api_key = await asyncio.to_thread(
            questionary.password(
                f"Paste your {env_var}:",
                style=_MINION_STYLE,
            ).ask
        )
        if not api_key or not api_key.strip():
            console.print(f"\n  [dim {SILVER}]No key entered. Run [bold]minion setup[/] to configure later.[/]\n")
            return False

        api_key = api_key.strip()

        # Step 3 — write to ~/.minion/.env (user-level, shared across all projects)
        env_path = Path.home() / ".minion" / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        _write_env_key(env_path, env_var, api_key)

        # Populate current process environment so the session works immediately
        import os
        os.environ[env_var] = api_key

        console.print()
        console.print(f"  [bold green]✓[/]  {env_var} saved to [bold]{env_path}[/]")
        console.print(f"  [dim {SILVER}]This key is shared across all projects. Edit [bold]~/.minion/.env[/] or run [bold]minion setup[/] to change it.[/]")
        console.print()

        # Step 4 — optional shell completion (best-effort, skip if shell undetectable)
        try:
            import shellingham
            shell_name, _ = shellingham.detect_shell()

            install_completion = await asyncio.to_thread(
                questionary.confirm(
                    f"Install shell tab completion for {shell_name}?",
                    default=True,
                    style=_MINION_STYLE,
                ).ask
            )

            if install_completion:
                from typer._completion_shared import install as _typer_install
                _, comp_path = _typer_install(shell=shell_name, prog_name="minion")
                console.print(f"  [bold green]✓[/]  Completion installed to [bold]{comp_path}[/]")
                console.print(f"  [dim {SILVER}]Restart your terminal to activate it.[/]")
                console.print()
            elif install_completion is not None:
                console.print(f"  [dim {SILVER}]Skipped. Run [bold]minion --install-completion[/] anytime to enable it.[/]")
                console.print()

        except Exception:
            pass  # Shell undetectable or install failed — not a wizard-blocking issue

        return True

    except (KeyboardInterrupt, EOFError):
        console.print(f"\n  [dim {SILVER}]Setup cancelled. Run [bold]minion setup[/] to configure later.[/]\n")
        return False


def _write_env_key(env_path: Path, key: str, value: str) -> None:
    """Write or update a key=value line in an .env file.

    Preserves existing content. If the key already exists, updates its value.
    If the file doesn't exist, creates it.
    """
    if env_path.exists():
        lines = env_path.read_text().splitlines(keepends=True)
        updated = False
        new_lines = []
        for line in lines:
            if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                new_lines.append(f"{key}={value}\n")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"{key}={value}\n")
        env_path.write_text("".join(new_lines))
    else:
        env_path.write_text(f"{key}={value}\n")
