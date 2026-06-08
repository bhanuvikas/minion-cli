# minion-cli

[![PyPI version](https://img.shields.io/pypi/v/minion-cli.svg)](https://pypi.org/project/minion-cli/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A terminal-native agentic coding assistant powered by LLMs. Reads, writes, and reasons
about code using a ReAct loop — with tool use, memory, reflection, skills, MCP server
integration, subagents, and agent-to-agent (A2A) coordination, all from a single CLI.

---

## Why minion-cli?

- **Full ReAct loop** — reads files, writes code, runs shell commands, and reasons across up to 20 iterations without you driving it step by step
- **Long-term memory** — extracts facts from sessions and retrieves them automatically; survives context compaction
- **MCP-native** — connect any MCP-compliant tool server; tools appear in the agent's tool list automatically
- **Full-screen TUI** — [Textual](https://textual.textualize.io/) terminal UI with scrollable conversation, live agent/tool status, subagent inspector (Ctrl+O), and inline permission panels
- **Multi-provider** — Anthropic, OpenAI, and OpenRouter; switch with a single flag or env var

---

## Prerequisites

- Python 3.11 or later
- One of the following API keys:
  - `ANTHROPIC_API_KEY` — [Anthropic Console](https://console.anthropic.com/settings/keys)
  - `OPENAI_API_KEY` — [OpenAI Platform](https://platform.openai.com/api-keys)
  - `OPENROUTER_API_KEY` — [OpenRouter](https://openrouter.ai/keys)

---

## Installation

```bash
pip install minion-cli
```

After installing, run the setup wizard to configure your API key and preferred model:

```bash
minion setup
```

### Shell tab completion (optional but recommended)

```bash
minion --install-completion zsh     # or bash, fish
```

Run `exec $SHELL` (or open a new terminal) to activate completion.

---

## Quickstart

```bash
# Check installed version
minion --version

# One-shot query
minion "explain what this repo does"

# Interactive REPL (full-screen TUI when stdout is a TTY)
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
reflect_depth      = 0          # 0 = off; 1–3 = self-refine rounds after each response
verbose            = false      # show critique text and response diffs during reflection
debug              = false      # print system prompt before each turn
agents_enabled     = true
max_subagent_depth = 2
approval_mode      = "off"      # "off" | "edits" (auto-approve writes) | "yolo" (auto-approve all)
markdown_enabled   = true       # render LLM responses as live markdown

[memory]
enabled              = true     # extract and inject cross-session memories
extraction_trigger   = "substantial"  # always | substantial | manual

[context]
auto_compact = true             # auto-compact when the input-token limit is hit

[tracing]
enabled = true                  # write session traces to ~/.minion/traces/

[hooks]
enabled           = true        # master on/off switch for all lifecycle hooks
builtin_minion_md = true        # tip to update MINION.md after file writes
```

**Priority:** CLI flag > environment variable > config.toml > built-in default.

### Environment variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `OPENROUTER_BASE_URL` | OpenRouter base URL (default: `https://openrouter.ai/api/v1`) |
| `MINION_PROVIDER` | Default provider: `anthropic` \| `openai` \| `openrouter` |
| `MINION_MODEL` | Default model ID (overridden by `--model` flag) |
| `MINION_NO_TUI` | Set to `1` to force console mode (disables the full-screen TUI) |

API keys and env vars belong in a `.env` file in your project directory, or exported from your shell:

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
MINION_MODEL=claude-sonnet-4-6
```

---

## Features

| Feature | How to use |
|---------|-----------|
| **One-shot mode** | `minion "your task"` |
| **Interactive REPL** | `minion` (no args); full-screen TUI when stdout is a TTY |
| **Tool use (ReAct loop)** | Automatic — reads/writes files, runs shell, searches code |
| **Self-refine reflection** | `--reflect 1` flag or `/config reflect 1` in REPL |
| **Long-term memory** | Automatic extraction/injection; `/remember`, `/forget`, `/memories` |
| **Skills** | `/commit`, `/refactor`, `/review`, `/explain`, `/test` |
| **Planning** | `/plan <goal>` — explore codebase → generate plan → execute |
| **Subagents** | `minion agents run coder "implement X"` or `/agent <role> <task>` |
| **MCP servers** | Configure in `~/.minion/mcp.json`; any MCP-compliant server |
| **A2A coordination** | `minion remote serve` — expose minion as a remote A2A agent |
| **Session tracing** | Automatic JSONL traces; `nefario --latest` to view in browser |
| **Dry-run mode** | `--dry-run` — preview tool calls without executing |
| **Auto-approval** | `/config approval edits` (writes only) or `yolo` (all tools) |
| **`@mention` injection** | `@path/to/file.py` in a message inlines the file into context |

---

## REPL Slash Commands

```
/help                                     List all commands
/init                                     Create a MINION.md template in the current directory
/setup                                    Re-run the setup wizard (model, completion, preferences)
/model                                    Switch provider, model, or API key interactively
/context                                  Show context window usage and token breakdown
/config [show|reflect|verbose|debug|      View or change a runtime setting
        markdown|approval|agents] [arg]
/hooks [list|on|off]                      Manage lifecycle hooks
/remember [--global] [--category] <text>  Store a memory fact
/forget <id or text>                      Delete a memory by ID or content match
/memories [query]                         Browse, search, edit, and delete stored memories
/compact [summary|truncate [N]]           Summarise or truncate conversation history
/clear                                    Wipe conversation history (keep session)
/save <name>                              Save current session to disk
/load <name>                              Restore a saved session by name
/resume                                   Pick a saved session from a dropdown
/plan <goal>                              Create a structured plan
/plan --execute [file]                    Execute the current (or named) plan
/plan --list                              List saved plans
/plan --clear                             Delete the active plan
/mcp [resource <uri>|prompt <name>|       Inspect MCP resources/prompts or reload servers
     reload]
/agents                                   List available agent roles
/agent <role> [task]                      Run a named role; omit task for persistent chat mode
/back                                     Exit agent chat mode (conversation not shared with minion)
/handoff                                  Exit agent chat mode and share summary with minion
/skills                                   Browse, run, create, and edit skill workflows
/remote [list|run <agent> <task>]         List or invoke configured remote A2A agents
/<skill-name>                             Run a named skill (e.g. /commit, /review, /explain)
/quit  /exit                              Exit Minion
```

---

## CLI Reference

### Root flags

```
minion [OPTIONS] [PROMPT]
```

| Flag | Short | Description |
|------|-------|-------------|
| `--provider` | `-p` | LLM provider: `anthropic` \| `openai` \| `openrouter` |
| `--model` | `-m` | Model ID (overrides `MINION_MODEL`) |
| `--dry-run` | | Show tool calls without executing |
| `--reflect N` | | Enable self-refine reflection (depth 1–3) |
| `--verbose` | `-v` | Show critique text and response diffs |
| `--no-memory` | | Disable memory for this session (private mode) |
| `--debug` | | Print system prompt before each turn |
| `--no-trace` | | Disable JSONL session tracing |
| `--version` | | Show version and exit |
| `--install-completion` | | Install shell tab completion (bash/zsh/fish) |

### Subcommands

| Command | Description |
|---------|-------------|
| `minion setup` | Interactive first-run wizard: provider, API key, preferences |
| `minion doctor` | Health check: API keys, memory, MCP servers, A2A agents |
| `minion config` | Show merged effective config (all sources) |
| `minion model` | Interactively configure provider, model, and API keys |
| `minion skills list` | List all available skills (builtin, user, project) |
| `minion mcp list` | List connected MCP servers and their tools |
| `minion agents list` | List agent roles with descriptions and tool subsets |
| `minion agents run <role> <task>` | One-shot subagent — roles: `researcher`, `coder`, `reviewer`, `tester` |
| `minion remote list` | List configured remote A2A agents |
| `minion remote serve [--port N]` | Start an A2A HTTP server (default port: 8080) |
| `minion memory recall [query]` | Search stored memories |
| `minion memory add <text>` | Store a memory (`--global` for global scope, `--category` for type) |
| `minion memory forget <query>` | Delete memories by ID prefix or content match |

---

## Nefario — Trace Viewer

Every session writes a JSONL trace to `~/.minion/traces/`. `nefario` is a second CLI
entry point that replays these traces in a self-contained browser-based viewer.

```bash
nefario                    # list recent sessions
nefario --latest           # open most recent session (default port: 7331)
nefario <session-id>       # open a specific session
nefario --port 8080        # use a different port
```

---

## Project-Level Configuration

Place a `MINION.md` file in your project root to inject context into minion's system prompt:

```markdown
# MINION.md

This is a FastAPI + PostgreSQL REST API. Use pytest for tests.
Never run database migrations without confirming with the user.
```

The `.minion/` directory in your project root holds project-scoped configuration that
overrides or extends `~/.minion/` global settings:

| Path | Contents |
|------|----------|
| `.minion/config.toml` | Project-level config (merged over `~/.minion/config.toml`) |
| `.minion/skills/` | Project-specific skill YAML files |
| `.minion/agents/` | Project-specific agent role overrides |
| `.minion/mcp.json` | Project-scoped MCP server configuration |
| `.minion/a2a.json` | Project-scoped remote A2A agent configuration |
| `.minion/plans/` | Saved plan files |
| `.minion/memory/` | Project-scoped memory records and vector index |
| `.minion/permissions.toml` | Project-level auto-approval rules |

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

# Type checking
pyright minion/

# Build wheel (packaging check)
python -m build
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
