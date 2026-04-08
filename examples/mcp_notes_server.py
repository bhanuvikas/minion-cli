#!/usr/bin/env python3
"""Example MCP server — notes storage.

A self-contained MCP server that exposes four tools for reading and writing
simple text notes to ~/.minion/notes/. Implements the MCP stdio transport
(JSON-RPC 2.0 over stdin/stdout) with no external dependencies.

This file is designed to be read alongside minion/mcp/client.py so you can
see both sides of the protocol in the same codebase.

Usage:
    # Register in .minion/mcp.json:
    {
      "servers": {
        "notes": {
          "command": ["python", "examples/mcp_notes_server.py"],
          "env": {}
        }
      }
    }

    # Then in minion REPL:
    > Create a note called "ideas" with the content "Build a banana-themed OS"
    > List all my notes
    > Read the note called "ideas"

How the protocol works (MCP over stdio):
    1. minion spawns this process as a subprocess
    2. minion writes JSON-RPC requests to our stdin (one per line)
    3. we write JSON-RPC responses to stdout (one per line)
    4. stderr is not read by minion — you can log debug info there

Initialize handshake:
    minion → {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
    us     → {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":...,"serverInfo":...}}
    minion → {"jsonrpc":"2.0","method":"notifications/initialized"}   (no id = notification)
    [no response needed for notifications]
    minion → {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    us     → {"jsonrpc":"2.0","id":2,"result":{"tools":[...]}}

Tool call:
    minion → {"jsonrpc":"2.0","id":3,"method":"tools/call",
               "params":{"name":"create_note","arguments":{"title":"ideas","content":"..."}}}
    us     → {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"Created!"}],"isError":false}}
"""

import json
import sys
from pathlib import Path

# ── Storage ────────────────────────────────────────────────────────────────────

NOTES_DIR = Path.home() / ".minion" / "notes"
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "minion-notes", "version": "0.1.0"}


def _slug(title: str) -> str:
    """Convert a note title to a safe filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in title.strip().lower()) or "untitled"


def _note_path(title: str) -> Path:
    return NOTES_DIR / f"{_slug(title)}.txt"


# ── Tool implementations ───────────────────────────────────────────────────────

def create_note(title: str, content: str) -> str:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = _note_path(title)
    path.write_text(content, encoding="utf-8")
    return f"Created note '{title}' at {path}"


def read_note(title: str) -> str:
    path = _note_path(title)
    if not path.exists():
        return f"Note '{title}' not found. Use list_notes to see available notes."
    return path.read_text(encoding="utf-8")


def list_notes() -> str:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(NOTES_DIR.glob("*.txt"))
    if not files:
        return "No notes found. Create one with create_note."
    return "\n".join(f.stem for f in files)


def delete_note(title: str) -> str:
    path = _note_path(title)
    if not path.exists():
        return f"Note '{title}' not found."
    path.unlink()
    return f"Deleted note '{title}'."


# ── Tool schema (returned by tools/list) ──────────────────────────────────────
# Note: MCP spec uses camelCase 'inputSchema'. The minion client converts this
# to 'input_schema' when building Anthropic API tool definitions.

TOOLS = [
    {
        "name": "create_note",
        "description": "Create or overwrite a text note with the given title and content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Note title (used as filename)"},
                "content": {"type": "string", "description": "Text content of the note"},
            },
            "required": ["title", "content"],
        },
        # No destructiveHint=True here — create_note can overwrite, but it's
        # expected behavior. Set annotations.destructiveHint=true on tools like
        # delete_note that permanently remove data.
    },
    {
        "name": "read_note",
        "description": "Read the content of an existing note by title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the note to read"},
            },
            "required": ["title"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_notes",
        "description": "List the titles of all saved notes.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "delete_note",
        "description": "Permanently delete a note by title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the note to delete"},
            },
            "required": ["title"],
        },
        # destructiveHint=True causes minion to ask for user confirmation before
        # calling this tool — same as write_file and run_shell for native tools.
        "annotations": {"destructiveHint": True},
    },
]


# ── Tool dispatch ──────────────────────────────────────────────────────────────

def call_tool(name: str, args: dict) -> tuple[str, bool]:
    """Execute a tool. Returns (result_text, is_error)."""
    try:
        if name == "create_note":
            return create_note(args["title"], args["content"]), False
        elif name == "read_note":
            return read_note(args["title"]), False
        elif name == "list_notes":
            return list_notes(), False
        elif name == "delete_note":
            return delete_note(args["title"]), False
        else:
            return f"Unknown tool: '{name}'", True
    except KeyError as e:
        return f"Missing required argument: {e}", True
    except Exception as e:
        return f"Error executing {name}: {e}", True


# ── JSON-RPC protocol ─────────────────────────────────────────────────────────

def _write(obj: dict) -> None:
    """Write one JSON-RPC message to stdout (newline-terminated)."""
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _read() -> dict | None:
    """Read one JSON-RPC message from stdin. Returns None on EOF."""
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def _respond(req_id, result) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


# ── Main server loop ──────────────────────────────────────────────────────────

def main() -> None:
    while True:
        try:
            req = _read()
        except json.JSONDecodeError as e:
            # Bad JSON — can't respond meaningfully without a valid id
            print(f"[notes-server] JSON parse error: {e}", file=sys.stderr)
            continue

        if req is None:
            # EOF: minion closed our stdin. Exit cleanly.
            break

        method = req.get("method", "")
        req_id = req.get("id")  # None for notifications

        if method == "initialize":
            _respond(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })

        elif method == "notifications/initialized":
            pass  # notification — no response expected

        elif method == "tools/list":
            _respond(req_id, {"tools": TOOLS})

        elif method == "tools/call":
            params = req.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            text, is_error = call_tool(tool_name, arguments)
            _respond(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            })

        elif req_id is not None:
            # Unknown method with an id — return an error
            _error(req_id, -32601, f"Method not found: {method}")

        # Unknown notifications (no id) are silently ignored per JSON-RPC spec


if __name__ == "__main__":
    main()
