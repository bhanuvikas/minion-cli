# minion-cli

[![PyPI version](https://img.shields.io/pypi/v/minion-cli.svg)](https://pypi.org/project/minion-cli/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A terminal-native agentic coding harness — streaming ReAct loop with reflection
and planning, MCP/A2A protocols, cross-session semantic memory, subagent
orchestration, YAML skills and agent roles, shell lifecycle hooks, and a Textual TUI.

---

## Highlights

- **ReAct loop** — runs up to 20 think→tool→observe iterations; `--reflect N` adds a separate critique→score→refine pass on each response, which is a distinct second LLM call, not part of the loop itself.
- **Planning mode** — `/plan <goal>` explores the codebase read-only first using a restricted tool subset, produces an editable markdown document, then executes with full tool access only after you approve it.
- **Long-term memory** — retrieves candidates scored as `0.7×similarity + 0.2×recency + 0.1×type_weight`; identity and preference records always inject, episodic records compete on score; stored as Markdown with YAML frontmatter you can edit directly.
- **Skills** — the five builtins (`/commit`, `/review`, `/refactor`, `/explain`, `/test`) are examples; drop any `.yaml` file into `.minion/skills/` to define `/yourthing` as a reusable prompt workflow.
- **Agent roles** — `researcher` is read-only, `coder` reads and writes but has no shell access, `tester` gets full tool access; each role runs in an isolated conversation with no access to REPL history.
- **Persistent agent sessions** — `/agent <role>` without a task opens an ongoing chat with that role; `/handoff` passes the conversation summary back to the main orchestrator as context.
- **Lifecycle hooks** — drop a YAML file in `.minion/hooks/` (project) or `~/.minion/hooks/` (global); the shell handler receives event JSON on stdin; exit 2 blocks the tool call before it runs; six event types cover the full session and tool lifecycle.
- **3-tier registries** — skills, agent roles, and permissions all resolve builtin → `~/.minion/` → `.minion/`; project-local entries shadow user and builtin ones without forking.
- **Dry-run and approval tiers** — `--dry-run` previews every tool call before touching anything; permanent rules live at session, project, or global scope; compound shell commands are split before pattern matching.
- **MCP and A2A** — persistent stdio/HTTP sessions per MCP server with no reconnect overhead per call; expose minion as an A2A endpoint any external orchestrator can delegate to, or delegate outward to named remote agents mid-task.
- **Nefario** — every session writes a JSONL trace to `~/.minion/traces/`; `nefario --latest` replays it in a self-contained browser SPA showing per-turn token counts and cache hit rates.
- **TUI modals** — `/agents`, `/skills`, `/hooks`, `/memories`, `/help`, `/config`, `/model`, and `/load` each open a full-screen modal; the entire system is browsable and configurable without leaving the terminal.

---

## Prerequisites

- Python 3.11 or later
- One of the following API keys:
  - `ANTHROPIC_API_KEY` — [Anthropic Console](https://console.anthropic.com/settings/keys) *(preferred)*
  - `OPENAI_API_KEY` — [OpenAI Platform](https://platform.openai.com/api-keys)
  - `OPENROUTER_API_KEY` — [OpenRouter](https://openrouter.ai/keys)

---

## Installation

```bash
pip install minion-cli
```

On first run, minion automatically detects that no API key is configured and walks you through setup.

---

## Quickstart

```bash
# One-shot query
minion "explain what this repo does"

# Interactive REPL (full-screen TUI when stdout is a TTY)
minion

# Check installed version
minion --version

# Health check
minion doctor
```

---

## Shell Tab Completion

```bash
minion --install-completion zsh     # or bash, fish
```

Run `exec $SHELL` (or open a new terminal) to activate.

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

---

## Features

| Feature | How to use |
|---------|-----------|
| **One-shot mode** | `minion "your task"` |
| **Interactive REPL** | `minion` (no args); full-screen TUI when stdout is a TTY |
| **File and code tools** | Automatic — `read_file`, `write_file`, `edit_file`, `list_directory`, `run_shell` |
| **Code navigation** | Automatic — `get_file_outline` extracts class/function structure from Python/JS/TS; `glob` finds files by name pattern; `search_file` greps by regex |
| **Web access** | Automatic — `web_fetch` retrieves and strips any URL the agent needs during a task |
| **Task tracking** | Automatic — agent uses `todo_write`/`todo_read` to maintain a visible checklist during multi-step work |
| **Self-refine reflection** | `--reflect 1` flag or `/config reflect 1` in REPL |
| **Long-term memory** | Automatic extraction/injection; `/remember`, `/forget`, `/memories` |
| **Skills** | `/commit`, `/refactor`, `/review`, `/explain`, `/test` — or write your own YAML skill |
| **Planning** | `/plan <goal>` — explore codebase → generate plan → execute |
| **Subagents** | `minion agents run coder "implement X"` or `/agent <role>` for persistent chat |
| **MCP servers** | Configure in `~/.minion/mcp.json`; tools, resources, and prompts; stdio and HTTP transports |
| **A2A coordination** | Both client and server — delegate to remote agents or accept tasks from external orchestrators |
| **Session tracing** | Automatic JSONL traces; `nefario --latest` to view in browser |
| **Dry-run mode** | `--dry-run` — preview tool calls without executing |
| **Auto-approval** | `/config approval edits` (writes only) or `yolo` (all tools) |
| **`@mention` injection** | `@path/to/file.py` in a message inlines the file into context |
| **Prompt caching** | Static system prompt sent with `cache_control: ephemeral` on Anthropic — cache hits visible in `nefario` traces as `cache_read_input_tokens` |

---

## Hooks

Hooks let you attach shell scripts to lifecycle events in the agent loop without touching minion's source code.
Drop a YAML file in `~/.minion/hooks/` (global) or `.minion/hooks/` (project-scoped).

### Events

| Event | Fires when |
|-------|-----------|
| `PreToolUse` | Before any tool call — can block execution |
| `PostToolUse` | After a tool call completes |
| `SessionStart` | When the REPL session initialises |
| `SessionEnd` | When the session exits cleanly |
| `UserPromptSubmit` | When the user submits a prompt |
| `StopTurn` | When the agent finishes a response turn |

### Shell handler protocol

The hook's command receives the event as a JSON object on **stdin**.

| Exit code | Meaning |
|-----------|---------|
| `0` | Proceed; stdout is parsed as `{"tip": "...", "reason": "..."}` (both optional) |
| `2` | Block (default for `PreToolUse`); stderr content is shown to the user as the reason |
| other | Non-blocking; execution continues regardless |

### Example hook

`~/.minion/hooks/audit-shell.yaml`:

```yaml
description: Append every shell command to an audit log
event: PreToolUse
tools: [run_shell]
command: |
  python3 -c "
  import sys, json
  d = json.loads(sys.stdin.read())
  open('/tmp/minion-audit.log', 'a').write(d['tool_input']['command'] + '\n')
  "
timeout: 5
```

`tools` is optional — omit it to match all tools. Set `blocking: true` to make the hook block on exit code 2 for non-`PreToolUse` events.

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

## Custom Skills and Agent Roles

### Skills

A skill is a YAML file that injects a system prompt and optional tool constraints before running the agent.
Place it in `.minion/skills/` (project) or `~/.minion/skills/` (user) and invoke it as `/<name>`.

`.minion/skills/deploy.yaml`:

```yaml
name: deploy
description: Run the deployment checklist and push to staging
prompt: |
  You are a deployment specialist.
  1. Run the test suite — stop if any tests fail.
  2. Run `git log --oneline origin/main..HEAD` to summarise what's shipping.
  3. Ask for confirmation before running the deploy command.
  4. Run: ./scripts/deploy.sh staging
tools:
  - run_shell
  - read_file
max_iterations: 10
```

Invoke with `/deploy` in the REPL. Builtin skills (`/commit`, `/review`, `/explain`, `/refactor`, `/test`)
follow the same format and can be used as templates. Skills can chain using a `steps:` list.

### Agent roles

An agent role defines a subagent with its own system prompt and a scoped tool list.
Place it in `.minion/agents/` (project) or `~/.minion/agents/` (user).

`.minion/agents/devops.yaml`:

```yaml
name: devops
description: Infrastructure and deployment specialist — has shell access, no file writes.
system_prompt: |
  You are a devops engineer. You inspect infrastructure, run diagnostics, and
  report findings. You may run read-only shell commands. Never modify files.
tools:
  - run_shell
  - read_file
  - list_directory
  - search_file
max_iterations: 15
```

Invoke with `/agent devops <task>` or `minion agents run devops "<task>"`.
Project roles shadow user roles; user roles shadow builtin roles on name collision.

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
