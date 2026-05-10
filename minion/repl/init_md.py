"""MINION.md generation — static template and LLM-streaming variant."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import ProjectContext
    from ..llm.base import LLMClient

_INIT_SYSTEM_PROMPT = """\
You are generating a MINION.md file — project instructions for an AI coding assistant called Minion.
Output ONLY the markdown content. No preamble, no explanation, no code fences.
Include these sections: a one-line project summary, ## How to run, ## How to test, \
## Key directories, ## Notes for Minion.
Keep it concise (under 40 lines). Base everything on the project context provided.
Rules:
- Always use relative paths (e.g. src/main.py), never absolute paths.
- In ## How to test: if no test files are detected, say so explicitly rather than guessing.
- If a value is genuinely unknown, write a short placeholder comment."""


def _generate_minion_md_llm(project_context: "ProjectContext", client: "LLMClient"):
    """Yield text chunks from LLM generation. Raises on stream error."""
    from ..llm.base import Message, TextChunk

    messages = [Message(
        role="user",
        content=f"Generate a MINION.md for this project:\n\n{project_context.to_prompt_block()}",
    )]
    stream = client.stream(messages, system=_INIT_SYSTEM_PROMPT)
    for event in stream:
        if isinstance(event, TextChunk):
            yield event.text


def _generate_minion_md(project_context: "ProjectContext | None") -> str:
    """Build a MINION.md starter template from detected project context."""
    manifest = project_context.manifest if project_context else None

    lines: list[str] = ["# MINION.md", ""]

    if manifest:
        label = manifest.language
        if manifest.framework:
            label += f" · {manifest.framework}"
        lines.append(f"Project instructions for Minion. This is a {label} project.")
    else:
        lines.append("Project instructions for Minion. Add anything the agent should know.")

    lines += ["", "## How to run"]
    if manifest and manifest.entry_point:
        lines.append(f"<!-- Entry point detected: {manifest.entry_point} -->")
    else:
        lines.append("<!-- e.g. python src/main.py / npm start / go run . -->")

    lines += [
        "",
        "## How to test",
        "<!-- e.g. pytest tests/ -q / npm test / go test ./... -->",
        "",
        "## Key directories",
        "<!-- Describe important directories and their purpose -->",
        "",
        "## Notes for Minion",
        "<!-- Conventions, things to avoid, important patterns -->",
        "<!-- Tip: create a .minionignore file to exclude paths from minion's file tree -->",
    ]

    return "\n".join(lines) + "\n"
