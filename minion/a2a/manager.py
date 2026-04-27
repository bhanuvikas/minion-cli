"""A2A manager — connects to all configured remote agents, routes send_task() calls.

One A2AClient per configured agent. Emits Nefario trace events around the
task lifecycle. Gracefully handles unknown agent names and connection failures.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ..theme import console
from ..tracing import get_tracer
from .client import A2AClient, A2AError
from .config import A2AAgentConfig, load_a2a_config
from .models import AgentCard


class A2AManager:
    """Routes named agent calls to the correct A2AClient.

    Created once at REPL startup. Loaded from a2a.json config (user + project
    tiers). Optionally fetches Agent Cards at startup for the /a2a list display.
    """

    def __init__(
        self,
        clients: dict[str, A2AClient],
        cards: dict[str, Optional[AgentCard]] = None,
    ) -> None:
        self._clients = clients
        self._cards: dict[str, Optional[AgentCard]] = cards or {}

    def agent_names(self) -> list[str]:
        return list(self._clients.keys())

    def has_agents(self) -> bool:
        return bool(self._clients)

    def agent_summary(self) -> list[dict]:
        """Return display info for each agent (name, url, card name/description)."""
        result = []
        for name, client in self._clients.items():
            card = self._cards.get(name)
            result.append({
                "name": name,
                "url": f"{client._scheme}://{client._netloc}",
                "card_name": card.name if card else "(unreachable)",
                "card_description": card.description if card else "",
            })
        return result

    def send_task(self, agent_name: str, task_text: str) -> str:
        """Send a task to the named remote agent and return the result text.

        Emits a2a_task_send, then a2a_task_complete or a2a_task_error.
        Returns an error string (never raises) so the LLM sees the error as
        a tool result rather than an uncaught exception.
        """
        client = self._clients.get(agent_name)
        if client is None:
            available = ", ".join(self._clients) if self._clients else "(none configured)"
            return (
                f"Error: unknown A2A agent '{agent_name}'. "
                f"Available: {available}"
            )

        remote_url = f"{client._scheme}://{client._netloc}"
        get_tracer().emit(
            "a2a_task_send",
            agent_name=agent_name,
            task=task_text,
            remote_url=remote_url,
        )

        start = time.monotonic()
        try:
            result = client.send_task(task_text)
            latency_ms = int((time.monotonic() - start) * 1000)
            get_tracer().emit(
                "a2a_task_complete",
                agent_name=agent_name,
                task=task_text,
                result=result[:500],
                result_length=len(result),
                latency_ms=latency_ms,
            )
            return result
        except A2AError as e:
            get_tracer().emit(
                "a2a_task_error",
                agent_name=agent_name,
                task=task_text,
                error=str(e),
            )
            return f"Error: {e}"


def load_a2a_manager(cwd: Path | None = None) -> A2AManager:
    """Load A2A config and construct one A2AClient per configured agent.

    Attempts to fetch each agent's Agent Card at startup so /a2a list can
    show descriptions. Card fetch failures are silent — the agent is still
    registered (it may be temporarily unreachable).
    """
    configs = load_a2a_config(cwd)
    clients: dict[str, A2AClient] = {}
    cards: dict[str, Optional[AgentCard]] = {}

    for name, cfg in configs.items():
        client = A2AClient(name=cfg.name, url=cfg.url, timeout_seconds=cfg.timeout_seconds)
        clients[name] = client
        cards[name] = client.fetch_agent_card()  # None if unreachable

    return A2AManager(clients=clients, cards=cards)
