"""Tool schemas sent to the LLM.

These are the first-class contracts between Minion and the model: the model reads
the descriptions to decide when and how to call each tool. Description quality
directly determines tool-call accuracy — keep them precise and action-oriented.

Intentionally separate from implementations.py: the LLM-facing schema and the
Python execution are two distinct concerns.
"""

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_file_outline",
        "description": (
            "Return the structure of a source file: class names, function names, and method "
            "names with their line numbers. Use this BEFORE read_file on any file longer than "
            "a few dozen lines — it lets you identify exactly which lines to read rather than "
            "loading the whole file. Supports Python (.py) and JavaScript/TypeScript (.js, .ts, "
            ".jsx, .tsx)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the source file.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search for a text pattern or regex across source files. Use this to locate "
            "a function definition, a class, a variable, an import, or any text without "
            "knowing which file it lives in. Returns filename:line_number:matched_line for "
            "each match. Prefer this over listing directories and reading files one by one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Text or regex pattern to search for. "
                        "Examples: 'def authenticate', 'class UserModel', 'import requests'."
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
                        "Examples: '*.py', '*.ts', '*.go'. Defaults to all files."
                    ),
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file with line numbers. "
            "Use start_line and end_line to read a specific range — ideal after "
            "get_file_outline tells you which lines a function spans. "
            "Without a range, files over 300 lines are truncated with a hint; "
            "use get_file_outline first to find the right range."
        ),
        "input_schema": {
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
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file, creating it if it doesn't exist or overwriting it completely "
            "if it does. Use this to create new files or replace file contents. "
            "Requires user confirmation before executing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "The complete content to write. Overwrites the entire file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List the files and subdirectories at a given path. Use this to explore project "
            "structure, find relevant files, or verify that a file was created. "
            "For finding where a function or class is defined, prefer search_code instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list. Defaults to current working directory.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "run_shell",
        "description": (
            "Execute a shell command and return its combined stdout and stderr output. "
            "Use this to run tests, check git status, install packages, build projects, "
            "or perform any terminal operation. Requires user confirmation before executing."
        ),
        "input_schema": {
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
    },
]

# Tools that modify state or execute arbitrary code — require user confirmation.
DANGEROUS_TOOLS: frozenset[str] = frozenset({"write_file", "run_shell"})
