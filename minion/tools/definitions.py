"""Tool schemas sent to the LLM.

These are the first-class contracts between Minion and the model: the model reads
the descriptions to decide when and how to call each tool. Description quality
directly determines tool-call accuracy — keep them precise and action-oriented.

Intentionally separate from implementations.py: the LLM-facing schema and the
Python execution are two distinct concerns.
"""

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file. Use this when you need to inspect source code, "
            "configuration, documentation, or any file to answer a question or complete a task. "
            "Large files are automatically truncated with a notice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file. Relative to the current working directory or absolute.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to read. Defaults to 50000.",
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
            "structure, find relevant files, or verify that a file was created."
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
