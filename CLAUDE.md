# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (once)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Tests
pytest tests/                                                                                      # full suite
pytest tests/test_display_utils.py                                                                 # single file
pytest "tests/test_display_utils.py::TestFragsToRichMarkup::test_rich_special_chars_escaped"      # single test
pytest --cov=minion --cov-report=term-missing                                                      # coverage report (exits non-zero if below 70% threshold)

# Run CLI (dev)
minion "your task"        # one-shot
minion                    # interactive REPL (TUI if stdout is a TTY)
MINION_NO_TUI=1 minion    # force console mode

# Build
python -m build
```

The venv Python interpreter is `.venv/bin/python` — use that when `python` / `pytest` isn't found.

## Architecture

### Execution flow

```
cli._entry()                  # intercepts one-shot prompts before typer routing
  └─ repl.run_repl_async()   # session setup → TUI/console bifurcation → input loop
       └─ runner.run_prompt_async()   # ReAct loop (up to 20 iterations)
            ├─ _stream_one_iteration_async()     # one LLM call → event stream
            └─ _execute_tools_async()            # route to parallel or single tool path
                 ├─ _execute_parallel_agents_async()   # spawn_agent / send_remote_task
                 ├─ _execute_parallel_tools_async()    # multiple generic tools in parallel
                 └─ executor.execute_async()           # single tool call
```

`run_prompt()` is a thin sync wrapper around `run_prompt_async()` via `asyncio.run()` — only safe to call from threads with no running event loop (agents use this).

### TUI / console bifurcation

In `repl.run_repl_async()`:
- `sys.stdout.isatty() and not MINION_NO_TUI` → `_run_repl_tui()` — full prompt_toolkit TUI (`tui/app.py`)
- Otherwise → `PromptSession` console loop (scrolling Rich output)

Both paths call `run_prompt_async()` with an `OutputRenderer`. The renderer is auto-detected at the start of `run_prompt_async()` and `ToolExecutor.__init__()` via `tui.get_tui_app()`.

### OutputRenderer abstraction

`output/base.py` defines the `OutputRenderer` ABC. Two implementations:
- `ConsoleRenderer` (`output/console.py`) — Rich console + live markdown streamer
- `TuiRenderer` (`output/tui.py`) — routes to `MinionApp`'s conversation buffer

Key contract: `parallel_display` property returns `None` on `ConsoleRenderer` (caller creates a new `ParallelDisplay`) and returns the shared `SlotsManager` on `TuiRenderer` (reuse to avoid nested Live conflicts). `needs_scrollback_flush=True` on `SlotsManager` means the caller must manually commit slot results to the TUI conversation buffer after parallel tasks finish.

### Parallel execution — thread-local display callbacks

`asyncio.TaskGroup` runs agents/tools concurrently. Each concurrent task sets `set_agent_display_callback(callback)` on a `threading.local` so text and tool-call events route to that task's slot without cross-contamination.

`_execute_parallel_tools_async()` detects whether it's running inside a `spawn_agent` by calling `get_agent_display_callback()`. If an outer callback is set (subagent mode), it uses `parallel_sub_*` events on the outer slot instead of creating a nested `Live` display. If not (standalone mode), it creates its own display and uses `_RenderBuffer` per tool to capture output for sequential scrollback replay after all tasks complete.

### Tool definitions vs implementations

`tools/definitions.py` — LLM-facing `ToolDefinition` schemas (name, description, JSON Schema). Description quality directly determines model accuracy.

`tools/implementations.py` — Python functions that implement each tool. All are synchronous; `ToolExecutor.execute_async()` runs them via `asyncio.to_thread()`.

`tools/executor.py` — dispatches: spawn_agent → `_agent_runner`, send_remote_task → `_remote_task_runner`, dangerous tools → confirmation, MCP tools (name contains `__`) → `MCPManager.call_tool()`, native → `_DISPATCH` table.

### Memory and prompt caching

`MemoryStore.retrieve()` runs vector/keyword search; results are formatted as `system_dynamic`. The static `system_prompt` (MINION.md + project context) is sent separately so Anthropic can cache it across turns. `system_dynamic` changes every turn and is excluded from caching to avoid wasted cache-write tokens.

### Subagent depth guard

`MAX_AGENT_DEPTH = 1` in `agents/runner.py`. Orchestrator depth=0 can spawn agents; workers at depth=1 have `spawn_agent` removed from their tool list. Prevents runaway recursive spawning.

### Confirmation

`ConfirmationManager` serializes confirmation dialogs across parallel threads via an asyncio lock. TUI path: `confirm_async()` (pauses/resumes the slot display). Non-TUI: `_interactive_confirm()` via questionary inside `asyncio.to_thread()`. `approval_mode="yolo"` bypasses confirmation; `"edits"` auto-approves only file writes.

### Key module boundaries

| Where to add | What goes there |
|---|---|
| `tools/definitions.py` | New LLM-facing tool schema |
| `tools/implementations.py` | New tool implementation |
| `tools/executor.py` | Dispatch, confirmation, hooks wiring |
| `runner/loop.py` | ReAct loop iteration logic |
| `runner/parallel.py` | Parallel execution strategies |
| `output/base.py` | New renderer event (add to ABC + both impls) |
| `display_utils.py` | Shared format-neutral `(style, text)` fragment helpers |
| `tui/slots.py` | Slot zone live display (TUI) |
| `agents/display.py` | `ParallelDisplay` (console Live) + thread-local callback registry |

### Testing patterns

Tests are pure unit tests — no API calls, no filesystem side effects. `monkeypatch` is used to stub external dependencies. See `tests/test_display_utils.py` and `tests/test_formatter.py` for examples. `pytest-asyncio` is configured with `asyncio_mode = "strict"` (tests must be explicitly marked `@pytest.mark.asyncio`).
