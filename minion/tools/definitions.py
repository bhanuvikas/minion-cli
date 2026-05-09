"""Tool schemas sent to the LLM.

These are the first-class contracts between Minion and the model: the model reads
the descriptions to decide when and how to call each tool. Description quality
directly determines tool-call accuracy — keep them precise and action-oriented.

Intentionally separate from implementations.py: the LLM-facing schema and the
Python execution are two distinct concerns.
"""

from ..llm.base import ToolDefinition

TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_file_outline",
        description=(
            "Return the structure of a source file: class names, function names, and method "
            "names with their line numbers. Use this BEFORE read_file on any file longer than "
            "a few dozen lines — it lets you identify exactly which lines to read rather than "
            "loading the whole file. Supports Python (.py) and JavaScript/TypeScript (.js, .ts, "
            ".jsx, .tsx)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the source file.",
                },
            },
            "required": ["path"],
        },
    ),
    ToolDefinition(
        name="search_file",
        description=(
            "Search for a text pattern or regex across files. Use this to locate "
            "a function definition, a class, a variable, a config value, or any text "
            "without knowing which file it lives in. Returns filename:line_number:matched_line "
            "for each match. Prefer this over listing directories and reading files one by one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Text or regex pattern to search for. "
                        "Examples: 'def authenticate', 'class UserModel', 'API_KEY'."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in. Defaults to current working directory.",
                },
                "file_glob": {
                    "type": "string",
                    "description": (
                        "Glob pattern to restrict which files are searched. "
                        "Examples: '*.py', '*.ts', '*.yaml'. Defaults to all text files."
                    ),
                },
            },
            "required": ["pattern"],
        },
    ),
    ToolDefinition(
        name="read_file",
        description=(
            "Read the contents of a file with line numbers. "
            "Use start_line and end_line to read a specific range — ideal after "
            "get_file_outline tells you which lines a function spans. "
            "Without a range, files over 300 lines are truncated with a hint; "
            "use get_file_outline first to find the right range."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to read (1-indexed, inclusive). Omit to start from line 1.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read (1-indexed, inclusive). Omit to read to end of file.",
                },
            },
            "required": ["path"],
        },
    ),
    ToolDefinition(
        name="write_file",
        description=(
            "Write content to a file. Use ONLY for:\n"
            "• Creating a new file (file does not exist yet).\n"
            "• Intentional full rewrites (e.g. generated boilerplate, config files).\n"
            "For editing an existing file, use edit_file instead — it is safer, "
            "cheaper, and cannot corrupt untouched parts of the file.\n"
            "Requires user confirmation before executing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
            },
            "required": ["path", "content"],
        },
    ),
    ToolDefinition(
        name="edit_file",
        description=(
            "Edit an existing file by replacing a specific block of text.\n"
            "Provide old_string (the exact text to find) and new_string (the replacement).\n"
            "Rules for old_string:\n"
            "• Must match the file content exactly — same whitespace, indentation, line endings.\n"
            "• Include 2–3 lines of surrounding context (before and after the change) so the "
            "match is unique within the file.\n"
            "• If old_string appears more than once, add more context until it is unique.\n"
            "Do NOT use this tool to create new files — use write_file for that.\n"
            "Requires user confirmation before executing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit.",
                },
                "old_string": {
                    "type": "string",
                    "description": (
                        "The exact text to find and replace. Must match the file content "
                        "exactly including whitespace. Include surrounding context lines "
                        "to ensure uniqueness."
                    ),
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text. Use an empty string to delete the matched block.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    ),
    ToolDefinition(
        name="list_directory",
        description=(
            "List the files and subdirectories at a single directory path. Use this to "
            "inspect a specific directory's immediate contents. "
            "To find files by name pattern across the whole project, use glob instead. "
            "To find where a function or value is defined, use search_file instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list. Defaults to current working directory.",
                },
            },
            "required": [],
        },
    ),
    ToolDefinition(
        name="glob",
        description=(
            "Find files whose paths match a glob pattern. Use this to locate files by name "
            "or extension without knowing exactly where they are. "
            "Supports ** for recursive matching across subdirectories. "
            "Examples: '**/*.py' (all Python files), 'src/**/*.ts' (TypeScript in src/), "
            "'**/test_*.py' (all test files), 'config.*' (any config file in root). "
            "Returns file paths relative to the search root. "
            "For searching file contents (not names), use search_file instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Glob pattern to match file paths. Use ** to match across directories. "
                        "Examples: '**/*.py', 'src/**/*.ts', '**/test_*.py'."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search from. Defaults to current working directory.",
                },
            },
            "required": ["pattern"],
        },
    ),
    ToolDefinition(
        name="web_fetch",
        description=(
            "Fetch the content of a URL and return it as plain text. "
            "Use this to read documentation, README files, API references, changelogs, or "
            "any web resource needed to complete a task. HTML is stripped to readable text. "
            "Responses are truncated at 50,000 characters. "
            "Requires user confirmation before executing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch. Must start with http:// or https://.",
                },
            },
            "required": ["url"],
        },
    ),
    ToolDefinition(
        name="run_shell",
        description=(
            "Execute a shell command and return its combined stdout and stderr output. "
            "Use this to run tests, check git status, install packages, build projects, "
            "or perform any terminal operation. Requires user confirmation before executing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds. Defaults to 30.",
                },
            },
            "required": ["command"],
        },
    ),
    ToolDefinition(
        name="send_remote_task",
        description=(
            "Delegate a task to a named remote A2A agent running on an external system. "
            "The agent runs its own reasoning loop independently and returns the result "
            "as text when done. "
            "Use when a task benefits from a specialized remote agent or external "
            "infrastructure. Available agents are listed in the system prompt. "
            "Do NOT use for tasks your local tools or local subagents can handle directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": (
                        "Name of the remote A2A agent to use (from a2a.json config). "
                        "Must match an available agent name exactly."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "Self-contained task description for the remote agent. Include all "
                        "context it needs — the remote agent has no access to the current "
                        "conversation history."
                    ),
                },
            },
            "required": ["agent", "task"],
        },
    ),
    ToolDefinition(
        name="spawn_agent",
        description=(
            "Spawn a specialized subagent to handle a focused subtask in isolation. "
            "The subagent runs its own ReAct loop with a dedicated context window and "
            "a tool subset matched to its role. Returns the subagent's complete response "
            "as a string when done. "
            "Available roles: researcher (read-only analysis), coder (implements features), "
            "reviewer (code review), tester (runs and diagnoses tests). "
            "Use when subtasks are genuinely independent and benefit from specialization or "
            "parallel execution. Do NOT use for simple questions you can answer directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Self-contained task description for the subagent. Include all "
                        "context it needs — the subagent has no access to the current "
                        "conversation history."
                    ),
                },
                "role": {
                    "type": "string",
                    "description": (
                        "Agent role to use: researcher, coder, reviewer, or tester. "
                        "Defaults to researcher if omitted."
                    ),
                },
            },
            "required": ["task"],
        },
    ),
    ToolDefinition(
        name="todo_write",
        description=(
            "Set or update the todo list for the current task. Replaces the entire list.\n"
            "Use at the start of any multi-step task (3+ steps) to set your plan, then call "
            "again to update statuses as you complete each step.\n"
            "Call todo_write(items=[]) to clear the list when the task is fully complete.\n"
            "statuses: 'pending' | 'in_progress' | 'done'"
        ),
        parameters={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text":   {"type": "string", "description": "Task description"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                        },
                        "required": ["text", "status"],
                    },
                    "description": "Full list of tasks. Replaces any previous list.",
                },
            },
            "required": ["items"],
        },
    ),
    ToolDefinition(
        name="todo_read",
        description="Return the current todo list. Use to check your plan before starting a new step.",
        parameters={"type": "object", "properties": {}, "required": []},
    ),
]

# Tools that modify state or execute arbitrary code — require user confirmation.
DANGEROUS_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "run_shell", "web_fetch"})

# Tools that produce side effects that cannot be undone (writes, shell execution).
# Reflection is skipped when any of these ran — the refiner cannot re-run tools,
# and the side effects have already occurred.
SIDE_EFFECTING_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "run_shell"})

# Delegation tools — both local subagents and remote A2A tasks are treated identically
# by the parallel execution path in runner.py.
DELEGATION_TOOLS: frozenset[str] = frozenset({"spawn_agent", "send_remote_task"})
