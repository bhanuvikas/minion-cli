# minion-cli

A terminal-native agentic coding assistant powered by LLMs. Reads, writes, and reasons
about code using a ReAct loop — with tool use, memory, reflection, skills, MCP server
integration, subagents, and agent-to-agent (A2A) coordination, all from a single CLI.

---

## Prerequisites

- Python 3.11 or later
- One of the following API keys:
  - `ANTHROPIC_API_KEY` — [Anthropic Console](https://console.anthropic.com/settings/keys)
  - `OPENAI_API_KEY` — [OpenAI Platform](https://platform.openai.com/api-keys)
  - `OPENROUTER_API_KEY` — [OpenRouter](https://openrouter.ai/keys)

---

## Installation

### From source (recommended until public release)

```bash
git clone https://github.com/bhanuvikas/minion-cli
cd minion-cli
pip install .
```

### Shell tab completion (optional but recommended)

```bash
minion --install-completion zsh     # or bash, fish
```

Run `exec $SHELL` (or open a new terminal) to activate completion.

---

## Quickstart

```bash
# First-time setup (runs automatically if no API key is found)
minion setup

# One-shot query
minion "explain what this repo does"

# Interactive REPL (full agent experience)
minion

# Health check
minion doctor
```

---

## Configuration

minion-cli reads `~/.minion/config.toml`. All settings have sensible defaults —
you don't need to create this file to get started.

```toml
[llm]
provider = "anthropic"          # anthropic | openai | openrouter
model    = "claude-sonnet-4-6"  # any model ID supported by the provider

[agent]
reflect_depth       = 0         # 0 = off; 1–3 = self-refine rounds after each response
agents_enabled      = true
max_subagent_depth  = 2

[memory]
enabled             = true      # extract and inject cross-session memories
extraction_trigger  = "substantial"  # always | substantial | manual

[tracing]
enabled = true                  # write session traces to ~/.minion/traces/
```

**Priority:** CLI flag > environment variable > config.toml > built-in default.

API keys belong in `.env` (or your shell environment), not config.toml:

```bash
# .env in your project directory (or export in your shell)
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Features

| Feature | How to use |
|---------|-----------|
| **One-shot mode** | `minion "your task"` |
| **Interactive REPL** | `minion` (no args) |
| **Tool use (ReAct loop)** | Automatic — reads/writes files, runs shell, searches code |
| **Reflection** | `--reflect 1` flag or `/reflect` in REPL |
| **Memory** | Automatic extraction/injection; `/remember`, `/forget` slash commands |
| **Skills** | `/commit`, `/refactor`, `/review`, `/explain`, `/test` |
| **Planning** | `/plan <goal>` — explore codebase → generate plan → execute |
| **SubAgents** | `minion agent run coder "implement X"` or `/agent` in REPL |
| **MCP servers** | Configure in `~/.minion/mcp.json`; any MCP-compliant server |
| **A2A server** | `minion a2a serve` — expose minion as a remote A2A agent |
| **Session tracing** | Automatic JSONL traces; `nefario --latest` to view |
| **Dry-run** | `--dry-run` — preview tool calls without executing |
| **Permissions** | `/yolo` (auto-approve all), `/edits` (auto-approve writes only) |

### REPL Slash Commands

```
/help          List all commands
/plan <goal>   Create and execute a structured multi-step plan
/compact       Summarize conversation to reclaim context window space
/reflect       Toggle self-critique and refinement mode
/yolo          Auto-approve all tool calls for this session
/edits         Auto-approve file writes only
/model         Switch LLM provider or model mid-session
/context       Show token usage breakdown by category
/clear         Wipe conversation history (keep session)
/save <name>   Save current session to disk
/load <name>   Restore a saved session
/remember <f>  Manually store a memory fact
/forget <p>    Delete memories matching a pattern
/agent <role>  Switch to an agent role for the next task
/skill <name>  Run a named skill directly
/a2a run <n>   Delegate a task to a remote A2A agent
/mcp reload    Reload MCP server connections without restarting
/config        Show current effective config (all sources merged)
/quit          Exit
```

---

## Project-Level Configuration

Place a `MINION.md` file in your project root to give minion context about your codebase:

```markdown
# MINION.md

This is a FastAPI + PostgreSQL REST API. Use pytest for tests.
Never run database migrations without confirming with the user.
```

Place skill YAML files in `.minion/skills/` for project-specific skills. Agent role
overrides go in `.minion/agents/`. MCP server config in `.minion/mcp.json`.

---

## Development Setup

```bash
git clone https://github.com/bhanuvikas/minion-cli
cd minion-cli
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage report
pytest --cov=minion --cov-report=term-missing

# Build wheel (packaging check)
python -m build
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
