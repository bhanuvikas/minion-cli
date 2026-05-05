# Changelog

All notable changes to minion-cli are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [1.0.0] — 2026-05-04 — "Gru's Lab"

### Added
- `pip install minion-cli` packaging via hatchling — wheel includes all YAML and HTML assets
- Shell tab completion via `minion --install-completion [bash|zsh|fish]`
- `minion setup` — interactive first-run onboarding wizard (provider + API key)
- First-run auto-detection: wizard fires automatically when no API key is configured
- CHANGELOG.md (this file) in keepachangelog format
- README.md with installation, quickstart, configuration reference, and feature tour
- Version sourced from package metadata via `importlib.metadata` (single source of truth)
- `pytest-cov` added to dev deps; 75%+ coverage target on core modules
- Flow tests for memory pipeline and planning cycle
- Unit tests for `minion/compact/` (previously uncovered)

### Fixed
- `pytest-asyncio` moved from runtime to dev dependencies
- `--install-completion zsh` no longer misinterpreted as a one-shot LLM prompt

---

## [0.12.0] — 2026-05-01 — "Turbo Mode"

### Added
- Full async rewrite: `run_prompt_async()` with `asyncio.TaskGroup` for parallel tools
- Official `mcp` Python SDK replaces ~1,200 lines of hand-rolled MCP protocol code
- `~/.minion/config.toml` via stdlib `tomllib` — unified settings (provider, model, memory, tracing)
- `/config` REPL command shows merged effective config from all sources
- `edit_file` tool — targeted old_string/new_string edits (no full-file rewrites)
- `glob` and `web_fetch` tools
- Prompt caching for tools and static system prompt (cache hits from turn 2 onward)
- 429 rate-limit retry (3× with 60s countdown) + auto-compact on input-token limit
- `/compact [summary|truncate [N]]` — strategy-pattern conversation compaction
- Ctrl+C cancellation with keep-completed-work (completed tool results preserved)
- `write_file` diff preview before overwrite
- Tool accumulation spinner showing pending calls
- Tiered permissions/trust model — session / project / global auto-approval tiers
- `todo_write` / `todo_read` tools for live task progress tracking
- Live Markdown rendering for LLM responses (`Rich.Live` + `Markdown`)
- REPL multiline input (Ctrl+J / Option+Enter inserts newline)
- Slash command highlighting anywhere in the input line
- Inline-editable `[enter custom]` in permission dialogs
- Banner redesign — figlet logo + two-column info panel, silver chrome theme
- Startup warnings deferred to section 3 (banner always renders first)
- `minion doctor` CLI subcommand — health check across API key, memory, MCP, A2A
- `/mcp reload` — live MCP server reload without REPL restart
- A2A context sessions (`contextId` per remote agent per session)
- Spec-compliant `input-required` task state for human approval in A2A

### Fixed
- Ctrl+C now cleanly cancels in-flight LLM calls (no zombie threads)
- Stdin drained before `questionary` prompts to prevent phantom declines
- `MemoryConfig` correctly wired from config.toml
- Consecutive user-message bug in `/compact` stub generation

---

## [0.11.0] — 2026-04-27 — "Global Domination"

### Added
- A2A (Agent-to-Agent) bidirectional protocol — minion as both client and server
- `minion a2a serve [--port N]` — expose minion as an HTTP A2A agent
- `minion a2a list` — list configured remote agents
- Agent Card served at `/.well-known/agent.json`
- `send_remote_task` tool for delegating to remote A2A agents from the REPL
- SSE streaming for A2A task subscriptions (`POST /tasks/sendSubscribe`)
- A2A config at `~/.minion/a2a.json` and `.minion/a2a.json`
- Nefario trace events for A2A task lifecycle

---

## [0.10.0] — 2026-04-25 — "Minion Army"

### Added
- SubAgent orchestrator-worker pattern via `spawn_agent` tool
- Parallel subagent execution with live terminal display panels
- 4 built-in agent roles: researcher, coder, reviewer, tester
- `minion agent run <role> <task>` — one-shot subagent CLI command
- `minion agent list` — list available roles with tool subsets
- Agent YAML manifests in `~/.minion/agents/` and `.minion/agents/`
- Max subagent depth limit (configurable via config.toml)
- Token aggregation across orchestrator + all workers in usage footer

---

## [0.9.0] — 2026-04-08 — "The Gadget Show"

### Added
- MCP (Model Context Protocol) client — connect any MCP-compliant tool server
- stdio and Streamable HTTP MCP transports
- MCP resources and prompt templates support
- `minion mcp list` — inspect connected servers and their tools
- Tool namespacing: `servername__toolname` format
- MCP config at `~/.minion/mcp.json` and `.minion/mcp.json`
- `/mcp prompt <name>` REPL command for interactive MCP prompt injection
- Nefario trace events for MCP tool calls and server notifications

---

## [0.8.0] — 2026-04-05 — "Gadgets"

### Added
- Skills system: reusable prompt templates invoked via `/skill-name`
- 5 built-in skills: `/commit`, `/explain`, `/refactor`, `/review`, `/test`
- User skills at `~/.minion/skills/` and project skills at `.minion/skills/`
- Skill chaining (steps field in skill YAML)
- `minion skills list` — enumerate all available skills
- Skill YAML manifests with system_prompt, tools, args, thinking_label fields

---

## [0.7.0] — 2026-04-03 — "Mission Planning"

### Added
- Document-based planning with explore → plan → refine → execute workflow
- `/plan <goal>` REPL command — creates a markdown plan with tool-assisted exploration
- Interactive refinement dropdown after plan creation
- `/plan execute` — inject plan into system prompt and run the ReAct agent
- `minion plan show` — display active plan
- Nefario observability system — SQLite event store + self-contained SPA trace viewer
- `nefario` CLI entry point (`nefario --latest` opens latest session trace)
- Plan files saved to `.minion/plans/` with datestamped slugs

---

## [0.6.0] — 2026-04-02 — "The Banana Vault"

### Added
- Long-term memory: extraction, storage, retrieval, and injection
- Episodic and semantic memory records with category classification
- Vector-based retrieval using numpy cosine similarity + keyword fallback
- `inject_memories()` appends a `## What I Remember` block to the system prompt
- `/remember <fact>` and `/forget <pattern>` slash commands
- Memory config: extraction trigger (always / substantial / manual)
- Memory token usage shown in `/context` breakdown
- Memory files at `~/.minion/memory/` with JSON vector index

---

## [0.5.0] — 2026-04-01 — "Banana Smoothie"

### Added
- Self-refine reflection loop: initial response → critique → score → refine
- `--reflect N` flag (depth 1–3) and `/reflect` REPL toggle
- Score threshold gating (only refines when score < threshold)
- Verbose mode shows critique text and response diffs
- LLM-assisted `/init` generates project-specific MINION.md from codebase structure

---

## [0.4.0] — 2026-04-01 — "Gelato"

### Added
- Code intelligence tools: `get_file_outline`, `search_file` (grep), `find_files`, `glob`
- MINION.md project context injection into system prompt
- `web_fetch` tool for fetching URLs during agent tasks
- `@file.py` mention syntax in REPL to inject specific files into context
- `/init` command to generate MINION.md for a project
- Tiered permission/trust model with session, project, and global scopes
- `/edits` and `/yolo` auto-approval modes

---

## [0.3.0] — 2026-03-31 — "Bello!"

### Added
- ReAct agent loop (Reason → Act → Observe → repeat, max 20 iterations)
- Tool use: `read_file`, `write_file`, `edit_file`, `list_directory`, `run_shell`
- Tool confirmation dialog before side-effecting operations
- `--dry-run` flag to preview tool calls without executing
- Provider-agnostic conversation model with typed `ContentBlock` hierarchy
- Parallel tool execution for multiple tool calls in a single LLM turn

---

## [0.2.0] — 2026-03-30 — "Papagena"

### Added
- Persistent conversation history within a session (messages array replay)
- `/save <name>` and `/load <name>` session commands
- Running token usage display with per-turn breakdown
- `ContextSnapshot` and `/context` command showing token budget by category
- Conversation compaction: LLM summary (default) and truncation strategies
- Sliding-window truncation as context limit fallback

---

## [0.1.0] — 2026-03-30 — "The Banana"

### Added
- MVP CLI: `minion "prompt"` for one-shot queries
- Interactive REPL mode (`minion` with no args)
- Minion theme: yellow/blue colors, figlet banner, "Bello!" greeting
- Multi-provider LLM support: Anthropic Claude, OpenAI, OpenRouter
- Streaming responses with live terminal output
- `python-dotenv` config via `.env`
- `/clear`, `/model`, `/quit`, `/help` slash commands
- Persistent REPL history (up/down arrow navigation)
- Tab completion for slash commands in REPL

---

[Unreleased]: https://github.com/bhanuvikas/minion-cli/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.12.0...v1.0.0
[0.12.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/bhanuvikas/minion-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/bhanuvikas/minion-cli/releases/tag/v0.1.0
