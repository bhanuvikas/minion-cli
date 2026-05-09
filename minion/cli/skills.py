"""minion skills — list and manage skill workflows."""

import typer

from ..theme import YELLOW, console

app = typer.Typer(name="skills", help="Manage Minion skills.", add_completion=False)


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    """List or manage skills. Run without subcommand to list all skills."""
    if ctx.invoked_subcommand is None:
        _list_skills()


@app.command("list")
def skills_list() -> None:
    """List all available skills (builtin, user, and project)."""
    _list_skills()


def _list_skills() -> None:
    from ..skills import load_skill_registry
    registry = load_skill_registry()
    for name, skill in registry.items():
        console.print(f"  [bold {YELLOW}]/{name:<14}[/] [{skill.source}] {skill.description}")
