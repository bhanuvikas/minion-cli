"""Agent Card generation for the A2A server.

The Agent Card is a JSON document served at /.well-known/agent.json that
advertises minion-cli's capabilities to external A2A orchestrators.
Generated dynamically at server startup so it always reflects the current
version, host, and port — no stale JSON file to maintain.
"""

from .. import __version__
from .models import AgentCard


def generate_agent_card(host: str, port: int) -> AgentCard:
    """Build an AgentCard for minion-cli's A2A server.

    The card describes minion as a coding assistant capable of reading,
    writing, analyzing code, running tests, and delegating to subagents.
    Skills listed are coarse capability buckets — not exhaustive.
    """
    return AgentCard(
        name="minion",
        description=(
            "Terminal agentic coding assistant with file I/O, shell execution, "
            "code intelligence, reflection, memory, skills, and subagents."
        ),
        url=f"http://{host}:{port}",
        version=__version__,
        capabilities={"streaming": True},
        skills=[
            {
                "id": "coding",
                "name": "Coding Assistant",
                "description": "Read, write, analyze, and refactor code files.",
            },
            {
                "id": "research",
                "name": "Research",
                "description": "Search and analyze codebases, find definitions and usages.",
            },
            {
                "id": "testing",
                "name": "Testing",
                "description": "Write, run, and diagnose test suites.",
            },
            {
                "id": "shell",
                "name": "Shell Execution",
                "description": "Run shell commands, build pipelines, check git status.",
            },
        ],
    )
