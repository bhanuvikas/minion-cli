# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (once)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Tests
pytest tests/                                                                                               # full suite
pytest tests/test_display_utils.py                                                                          # single file
pytest "tests/test_display_utils.py::TestFragsToRichMarkup::test_rich_special_chars_escaped"               # single test
pytest --cov=minion --cov-report=term-missing                                                               # coverage (exits non-zero below 70%)

# Run CLI (dev)
minion "your task"        # one-shot
minion                    # interactive REPL (TUI if stdout is a TTY)
MINION_NO_TUI=1 minion    # force console mode

# Build
python -m build
```

The venv Python interpreter is `.venv/bin/python` — use it when `python` / `pytest` isn't on PATH.

---

## Package layout

```
minion/
├── cli.py              # typer entry point — argument parsing only
├── repl.py             # REPL session — input loop, slash commands, session setup
├── theme.py            # global Rich console, colour palette, print helpers
│
├── llm/                # LLM clients and core data types
│   ├── base.py         # ABCs: LLMClient, streaming events, ToolDefinition, Message
│   ├── anthropic.py    # Anthropic streaming client
│   ├── openai.py       # OpenAI / OpenRouter client
│   ├── factory.py      # get_client() — resolves provider from env / CLI flag
│   ├── conversation.py # Conversation — message list, token tracking, ContextSnapshot
│   └── reflection.py   # Self-Refine loop (critique → refine LLM calls)
│
├── runner/             # ReAct loop execution
│   ├── loop.py         # run_prompt_async() — streams one LLM call, dispatches tools
│   ├── parallel.py     # asyncio.TaskGroup dispatch for agents and parallel tools
│   ├── context.py      # @mention resolution, message serialisation for tracing
│   └── session.py      # save/load Conversation to ~/.minion/sessions/
│
├── tools/              # Tool schema, execution, and access control
│   ├── definitions.py  # TOOL_DEFINITIONS — LLM-facing ToolDefinition list
│   ├── implementations.py  # sync Python functions for every native tool
│   ├── executor.py     # ToolExecutor — dispatch, hooks, confirmation, dry-run
│   ├── permissions.py  # PermissionStore — 3-tier allow-list (session/project/global)
│   └── confirmation.py # ConfirmationManager — serialised prompts across threads
│
├── output/             # Rendering abstractions and display utilities
│   ├── base.py         # OutputRenderer ABC + SlotSpec dataclass
│   ├── console.py      # ConsoleRenderer — Rich Live + markdown streaming
│   ├── tui.py          # TuiRenderer — routes events to MinionApp buffers
│   ├── formatter.py    # Rich markup builders: tool calls, results, summaries
│   ├── display_utils.py # Format-neutral (style, text) fragment helpers
│   └── diff.py         # compute_diff / format_diff_rich — standalone, no minion deps
│
├── tui/                # prompt_toolkit TUI (full-screen, TTY only)
│   ├── app.py          # MinionApp — owns the terminal, layout, keybindings
│   ├── conversation.py # ConversationBuffer — append-only scrollback widget
│   ├── slots.py        # SlotsManager — live agent/tool status zone
│   ├── inspector.py    # Ctrl+O subagent transcript inspector panel
│   ├── permission.py   # PermissionPanel — TUI confirmation widget
│   ├── render.py       # Stateless ANSI renderers for conversation lines
│   ├── agent_registry.py # In-memory registry of running subagents for inspector
│   ├── keys.py         # Keybinding definitions
│   ├── status.py       # Status bar widget
│   └── theme.py        # TUI-specific prompt_toolkit style
│
├── agents/             # Subagent spawning and role management
│   ├── runner.py       # run_agent() — spawns isolated subagent, returns response text
│   ├── registry.py     # AgentRegistry — 3-tier YAML loader (builtin/user/project)
│   ├── manifest.py     # AgentRoleManifest dataclass
│   └── display.py      # ParallelDisplay (console Live) + thread-local callback registry
│
├── memory/             # Persistent memory: store, extraction, retrieval
│   ├── store.py        # MemoryStore — coordinate retrieval, storage, consolidation
│   ├── extractor.py    # LLM-based memory extraction from turns
│   ├── embedder.py     # Sentence-transformer embedding (lazy-loaded)
│   ├── vector_store.py # Flat JSONL vector index with cosine similarity
│   ├── record.py       # MemoryRecord dataclass + disk I/O
│   ├── triggers.py     # ExtractionTrigger strategies (Substantial/EveryN/Always/Manual)
│   ├── injection.py    # Format retrieved records into system_dynamic string
│   └── config.py       # MemoryConfig dataclass
│
├── hooks/              # Lifecycle hook system
│   ├── events.py       # Frozen event dataclasses: PreToolUse, PostToolUse, SessionStart…
│   ├── runner.py       # HookRunner — dispatches events, isolates handler errors
│   ├── handler.py      # HookHandler ABC
│   ├── registry.py     # HookRegistry — builds HookRunner from MinionConfig
│   ├── result.py       # HookResult — block/allow/modify semantics
│   ├── handlers/shell.py  # ShellHandler — runs shell commands as hooks
│   └── builtin/minion_md.py  # Built-in: MINION.md staleness tip after file writes
│
├── context/            # Project context for the system prompt
│   ├── project.py      # ProjectContext + build_project_context() — called at startup
│   ├── manifest.py     # ProjectManifest — detects language, framework, toolchain
│   ├── filetree.py     # build_file_tree() — filtered directory listing
│   └── prompts.py      # BASE_SYSTEM_PROMPT + build_system_prompt()
│
├── config/             # User preferences and setup
│   ├── file.py         # load_config() / MinionConfig — reads ~/.minion/config.toml
│   ├── interactive.py  # MINION_STYLE, run_model_config() — /model questionary flow
│   └── wizard.py       # run_setup_wizard() — first-run API key setup
│
├── compact/            # Conversation compaction strategies
│   ├── base.py         # CompactionStrategy ABC + CompactionResult
│   ├── summary.py      # SummaryStrategy — LLM-generated conversation summary
│   └── truncate.py     # TruncateStrategy — drop oldest messages by token budget
│
├── planner/            # /plan command — explore-first structured planning
│   ├── creator.py      # create_plan(), execute_plan() — streaming planner loop
│   └── storage.py      # save/load plan markdown to .minion/plans/
│
├── skills/             # /skill YAML-defined reusable prompt workflows
│   ├── registry.py     # SkillRegistry — 3-tier YAML loader (builtin/user/project)
│   ├── runner.py       # execute_skill() — augments system prompt, calls run_prompt()
│   └── manifest.py     # SkillManifest dataclass
│
├── mcp/                # MCP (Model Context Protocol) server integration
│   ├── manager.py      # MCPManager — persistent async sessions, tool/prompt/resource API
│   └── config.py       # MCPServerConfig, load_mcp_config()
│
├── a2a/                # A2A (Agent-to-Agent) remote task delegation
│   ├── manager.py      # A2AManager — routes send_task() to the right A2AClient
│   ├── client.py       # A2AClient — HTTP calls, streaming, auth
│   ├── server.py       # A2A server — exposes this Minion instance as an agent
│   ├── config.py       # A2AAgentConfig, load_a2a_config()
│   ├── card.py         # AgentCard fetching
│   └── models.py       # A2A request/response dataclasses
│
└── tracing/            # JSONL session tracing (Nefario observability)
    ├── tracer.py       # Tracer / NullTracer — get_tracer(), init_tracer()
    ├── events.py       # Trace event constants
    ├── cli.py          # /trace CLI subcommand
    └── server.py       # Trace replay server
```

---

## Core execution flow

```
cli._entry()                     # scans sys.argv; one-shot prompt bypasses typer routing
  └─ repl.run_repl_async()       # session bootstrap → TUI/console split → input loop
       │   load config, build ProjectContext, init LLM client
       │   connect MCP servers, load hooks, build PermissionStore
       │   create ConfirmationManager
       │
       ├─ _run_repl_tui()        # TTY + not MINION_NO_TUI
       │    MinionApp.run_async() (prompt_toolkit full-screen)
       │
       └─ _run_repl_console()    # otherwise — PromptSession + Rich output
            └─ run_prompt_async()     # ReAct loop (up to MAX_ITERATIONS=20)
                 ├─ _stream_one_iteration_async()   # one LLM call → event stream
                 │    TextChunk → on_text_delta()
                 │    ToolUseBlock → accumulate
                 │    StreamComplete → capture usage, stop_reason
                 │
                 └─ _execute_tools_async()
                      ├─ single tool → executor.execute_async()
                      ├─ all delegation → _execute_parallel_agents_async()
                      └─ mixed/generic → _execute_parallel_tools_async()
```

`run_prompt()` is a thin `asyncio.run()` wrapper — only safe from threads with no running event loop (used by subagents).

---

## LLM layer (`llm/`)

**`llm/base.py`** defines all shared types:
- `LLMClient` ABC — `stream()`, `async_stream()`, `complete()` methods
- Streaming events: `TextChunk`, `ToolUseBlock`, `ToolAccumulationStart`, `StreamComplete`, `InputTokenRateLimitError`
- `Message(role, content)` — content is `str` (plain text) or `list[ContentBlock]` (tool turns)
- `ToolDefinition(name, description, parameters)` — provider-neutral; each adapter converts to its wire format

**Provider resolution** (`llm/factory.py`): `MINION_PROVIDER` env var → defaults to `"anthropic"`. Supported: `anthropic`, `openai`, `openrouter`.

**`InputTokenRateLimitError`** is the signal for the runner to compact the conversation (not retry after a delay — the context is the same size after 60s). Other rate limit errors use exponential backoff.

**Prompt caching**: `system` (static MINION.md + project context) is sent as a cacheable prefix. `system_dynamic` (per-turn memory results) is passed separately and never cached — it changes every turn.

---

## Tool system (`tools/`)

### Adding a new tool

1. **Schema** — add a `ToolDefinition` to `tools/definitions.py`. Description quality is everything; the model reads it to decide when and how to call the tool.
2. **Implementation** — add a sync Python function to `tools/implementations.py` and register it in `_DISPATCH`.
3. **Danger flag** — if the tool is side-effecting, add it to `DANGEROUS_TOOLS` in `executor.py`. It will then require confirmation (unless auto-approved by `approval_mode`).

### Dispatch order in `ToolExecutor.execute_async()`

1. `spawn_agent` → injected `_agent_runner` (runs `run_prompt()` in a thread)
2. `send_remote_task` → injected `_remote_task_runner` (A2A HTTP call)
3. `DANGEROUS_TOOLS` → `ConfirmationManager.confirm_sync()` (unless auto-approved)
4. Pre-tool hook → `HookRunner.fire(PreToolUseEvent)` — a `block` result stops execution
5. MCP tool (name contains `__`) → `MCPManager.call_tool()`
6. Native → `_DISPATCH[name](inputs)` in `asyncio.to_thread()`

### Permissions (`tools/permissions.py`)

`PermissionStore` has three tiers: session (in-memory), project (`.minion/permissions.toml`), global (`~/.minion/permissions.toml`). `is_trusted(tool, command)` checks all tiers. For `run_shell`, compound commands (`&&`, `;`) are split and each part must match independently.

### Confirmation (`tools/confirmation.py`)

`ConfirmationManager` serialises all confirmation dialogs via `threading.Lock` so parallel tool executions don't show overlapping prompts. TUI mode routes to `PermissionPanel`; console mode uses questionary inside `asyncio.to_thread()`.

---

## Parallel execution (`runner/parallel.py`)

Two parallel paths, selected by `_execute_tools_async()`:

**Delegation tools** (`spawn_agent` / `send_remote_task`):
- Each agent runs in `asyncio.to_thread()` (sync `run_prompt()` inside)
- `set_agent_display_callback(callback)` on a `threading.local` routes each agent's events to its own display slot
- `asyncio.TaskGroup` waits for all; results injected into the orchestrator's conversation

**Generic parallel tools**:
- **Standalone mode** (no outer callback): creates its own `ParallelDisplay`, each tool runs in a thread, `_RenderBuffer` captures each tool's renderer calls for sequential scrollback replay after all tasks complete
- **Subagent mode** (inside `spawn_agent`, outer callback set): emits `parallel_sub_*` events on the outer slot instead of creating a conflicting nested Live display

Subagent mode is detected by `get_agent_display_callback()` returning non-None.

---

## OutputRenderer abstraction (`output/`)

`output/base.py` defines the `OutputRenderer` ABC. Both implementations satisfy the same interface:

| Property/Method | `ConsoleRenderer` | `TuiRenderer` |
|---|---|---|
| `parallel_display` | `None` (caller creates `ParallelDisplay`) | shared `SlotsManager` |
| `needs_scrollback_flush` | `False` | `True` |
| `on_text_delta()` | writes to stdout | appends to `ConversationBuffer` |
| `spinner()` | `console.status()` context | no-op `nullcontext` |

When `needs_scrollback_flush=True`, the caller (in `runner/parallel.py`) must manually commit slot results to the TUI conversation buffer after all parallel tasks finish — `SlotsManager.clear()` removes the live zone.

**Display fragments**: `output/display_utils.py` provides `(style, text)` fragment helpers (`tool_slot_header_frags`, `apply_slot_event`, `frags_to_rich_markup`) shared by `agents/display.py`, `output/formatter.py`, `tui/render.py`, and `tui/slots.py`. Styles are hex colors + bold — compatible with both Rich and prompt_toolkit.

---

## Agents (`agents/`)

**Role manifests** (`agents/registry.py`): 3-tier YAML loader — builtin (`agents/builtin/`), user (`~/.minion/agents/`), project (`.minion/agents/`). Higher tiers shadow lower tiers on name collision.

**Depth guard**: `MAX_AGENT_DEPTH = 1` in `agents/runner.py`. Depth=0 orchestrators can spawn agents; depth=1 workers have `spawn_agent` removed from their tool list. Prevents recursive runaway.

**Subagent isolation**: `run_agent()` creates a fresh `Conversation`, calls `run_prompt()` with `capture_output=True`, and returns the full response text as a string injected into the orchestrator's conversation as a tool result.

---

## Memory (`memory/`)

**Retrieval** (`memory/store.py`):
1. Embed query → cosine search on both global and project vector indexes
2. Score = `0.7×similarity + 0.2×recency + 0.1×type_weight`
3. Pairwise consolidation: if two records exceed `consolidation_threshold`, call `extractor.consolidate()` to merge them

**Extraction triggers** (`memory/triggers.py`): strategy pattern — swap `ExtractionTrigger` subclass to change when extraction fires without touching `MemoryStore`.

**Prompt caching split**: retrieved records go into `system_dynamic` (not cached) so they don't consume cache-write tokens. The static system prompt stays cacheable.

**Storage layout**:
- Global: `~/.minion/memory/records/` + `~/.minion/memory/index.jsonl`
- Project: `.minion/memory/records/` + `.minion/memory/index.jsonl`

---

## Hooks (`hooks/`)

**Lifecycle events**: `PreToolUseEvent`, `PostToolUseEvent`, `SessionStartEvent`, `SessionEndEvent`, `UserPromptSubmitEvent` — all frozen dataclasses.

**`PreToolUse` blocks**: if any handler returns `HookResult(action="block")`, `ToolExecutor` skips execution and returns the block message as the tool result.

**Error isolation**: `HookRunner` swallows all handler exceptions — hooks never crash the agent loop.

**Configuration**: user hooks defined in `~/.minion/config.toml` (global) and `.minion/config.toml` (project) under `[hooks.user]`. See `config/file.py` for the `HookDefinition` schema.

**Built-in hooks**: `hooks/builtin/minion_md.py` fires after `write_file` / `edit_file` and suggests updating MINION.md if the project root file is stale.

---

## Context and prompts (`context/`)

**`context/project.py`**: `build_project_context()` called once at startup. Combines:
- `ProjectManifest` — detected language, framework, toolchain, key files
- File tree (filtered, respects `.gitignore`)
- `MINION.md` contents (if present in cwd)

**`context/prompts.py`**: `BASE_SYSTEM_PROMPT` (static identity/behaviour rules) + `build_system_prompt(project_context)`. The result is the session's immutable system prompt.

**`@mention` resolution** (`runner/context.py`): `@path/to/file.py` in a user message is expanded to include the file's contents inline. Only paths with `/` or an extension are resolved — bare `@property` is left unchanged.

---

## Config (`config/`)

| File | Purpose |
|---|---|
| `config/file.py` | `load_config()` — reads `~/.minion/config.toml` then `<cwd>/.minion/config.toml`, returns `MinionConfig` |
| `config/interactive.py` | `MINION_STYLE`, `run_model_config()` — questionary `/model` flow |
| `config/wizard.py` | `run_setup_wizard()` — first-run API key wizard |
| `config/__init__.py` | Re-exports everything; callers use `from .config import X` |

**Loading priority** (lowest → highest): hardcoded defaults → `~/.minion/config.toml` → `.minion/config.toml` → `.env` → CLI flags.

---

## Skills and planner

**Skills** (`skills/`): YAML-defined prompt workflows invoked as `/skill-name`. 3-tier registry (builtin / user `~/.minion/skills/` / project `.minion/skills/`). `execute_skill()` augments the base system prompt with the skill's instructions and calls `run_prompt()`. Skills can be chained via `steps:` list (cycle detection included).

**Planner** (`planner/`): `/plan <goal>` runs a read-only explore-first loop that produces a structured markdown plan document. `execute_plan()` injects the approved plan into the system prompt and runs the full ReAct agent.

---

## MCP and A2A

**MCP** (`mcp/`): each configured server gets a persistent `asyncio.Task` maintaining a live session. Tool calls reuse the live session — no reconnect per call. MCP tool names are namespaced with `__` (e.g. `server__tool_name`) for routing. Configured in `.minion/mcp.json` (project) or `~/.minion/mcp.json` (global).

**A2A** (`a2a/`): `send_remote_task` tool routes to `A2AManager` → `A2AClient` (HTTP streaming). Remote agents are declared in `.minion/a2a.json`. `a2a/server.py` exposes this Minion instance as an A2A-compatible endpoint.

---

## Compaction (`compact/`)

Triggered when `InputTokenRateLimitError` is raised (auto) or by `/compact` (manual).

- `SummaryStrategy` — calls the LLM to summarise the conversation, replaces all messages with a single summary message
- `TruncateStrategy` — drops oldest messages until under a token budget (preserves the system message and most recent turns)

Strategy is selected in `repl.py` based on `/compact truncate` argument.

---

## Tracing (`tracing/`)

`init_tracer(session_id)` called at session start. `get_tracer().emit(event_type, **kwargs)` anywhere — safe before init (returns `NullTracer`). Events written as JSONL to `~/.minion/traces/<session_id>.jsonl`. The `/trace` CLI subcommand can replay or tail traces.

---

## Where to add things

| Task | File(s) |
|---|---|
| New native tool | `tools/definitions.py` (schema) + `tools/implementations.py` (function + `_DISPATCH` entry) |
| Make a tool require confirmation | add to `DANGEROUS_TOOLS` in `tools/executor.py` |
| New renderer event | `output/base.py` (add to ABC) + `output/console.py` + `output/tui.py` |
| New slash command | `repl.py` — `_handle_slash_command()` |
| New agent role (builtin) | `agents/builtin/<role>.yaml` |
| New skill (builtin) | `skills/builtin/<skill>.yaml` |
| New hook event type | `hooks/events.py` (frozen dataclass) + `hooks/runner.py` (dispatch) |
| New hook handler type | implement `HookHandler` ABC, register in `hooks/registry.py` |
| New compaction strategy | implement `CompactionStrategy` ABC in `compact/`, wire in `repl.py` |
| New config setting | `config/file.py` (add to dataclass + `load_config()`) |
| Parallel display fragment | `output/display_utils.py` |
| TUI layout change | `tui/app.py` |
| Subagent slot display | `tui/slots.py` (TUI) and `agents/display.py` (console) |

---

## Testing patterns

Tests are pure unit tests — no real API calls, no persistent filesystem writes. Use `monkeypatch` or `unittest.mock` to stub external dependencies. `tmp_path` fixture for any filesystem I/O.

```python
# Async tests must be explicitly marked (asyncio_mode = "strict" in pyproject.toml)
@pytest.mark.asyncio
async def test_something():
    ...

# Patch internal module paths, not the import source
with patch("minion.runner.loop.console"):  # not "rich.console.Console"
    ...

# Patch module-level state (e.g. SESSIONS_DIR)
with patch("minion.runner.session.SESSIONS_DIR", tmp_path):
    ...
```

Reference test files: `tests/test_display_utils.py`, `tests/test_formatter.py`, `tests/test_executor.py`, `tests/test_reflection.py`.
