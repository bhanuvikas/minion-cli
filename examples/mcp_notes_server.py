#!/usr/bin/env python3
"""Example MCP server — notes storage.

A self-contained MCP server that exposes:
  - 4 tools for reading and writing notes to ~/.minion/notes/
  - Resources: each saved note is a URI-addressable resource (notes://title)
  - Prompt templates: reusable prompts that inject note context into conversations

Implements the MCP stdio transport (JSON-RPC 2.0 over stdin/stdout) with no
external dependencies. Designed to be read alongside minion/mcp/client.py so
you can see both sides of the protocol.

Usage:
    # Register in .minion/mcp.json:
    {
      "servers": {
        "notes": {
          "command": ["python", "/absolute/path/to/examples/mcp_notes_server.py"],
          "env": {}
        }
      }
    }

    # Then in minion REPL:
    > Create a note called "ideas" with the content "Build a banana-themed OS"
    > List all my notes
    > Read the note called "ideas"

    # Use MCP commands to browse resources and invoke prompt templates:
    > /mcp
    > /mcp resource notes://ideas
    > /mcp prompt notes__summarize_notes
    > /mcp prompt notes__find_related topic=AI

MCP protocol summary (stdio transport):
    1. minion spawns this process as a subprocess
    2. minion writes JSON-RPC requests to our stdin (one per line)
    3. we write JSON-RPC responses to stdout (one per line)
    4. stderr is not read by minion — you can log debug info there

Initialize handshake:
    minion → {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
    us     → {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":...,"serverInfo":...,"capabilities":{...}}}
    minion → {"jsonrpc":"2.0","method":"notifications/initialized"}
    minion → {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    us     → {"jsonrpc":"2.0","id":2,"result":{"tools":[...]}}
    minion → {"jsonrpc":"2.0","id":3,"method":"resources/list"}
    us     → {"jsonrpc":"2.0","id":3,"result":{"resources":[...]}}
    minion → {"jsonrpc":"2.0","id":4,"method":"prompts/list"}
    us     → {"jsonrpc":"2.0","id":4,"result":{"prompts":[...]}}

Resource read:
    minion → {"jsonrpc":"2.0","id":5,"method":"resources/read","params":{"uri":"notes://ideas"}}
    us     → {"jsonrpc":"2.0","id":5,"result":{"contents":[{"uri":"notes://ideas","mimeType":"text/plain","text":"..."}]}}

Prompt get:
    minion → {"jsonrpc":"2.0","id":6,"method":"prompts/get","params":{"name":"summarize_notes","arguments":{}}}
    us     → {"jsonrpc":"2.0","id":6,"result":{"messages":[{"role":"user","content":{"type":"text","text":"..."}}]}}
"""

import json
import sys
from pathlib import Path

# ── Storage ────────────────────────────────────────────────────────────────────

NOTES_DIR = Path.home() / ".minion" / "notes"
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "minion-notes", "version": "0.2.0"}

# Advertise tools, resources, and prompts capabilities.
# minion's client only calls resources/list and prompts/list if these keys are present.
CAPABILITIES = {"tools": {}, "resources": {}, "prompts": {}}


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


# ── Resource implementations ───────────────────────────────────────────────────

def list_resources() -> list[dict]:
    """Return MCP resource descriptors for all saved notes.

    Each note is exposed as a URI-addressable resource:
        notes://ideas  →  the note saved as ideas.txt
    Resources are read-only data; the LLM reads them for context without calling a tool.
    """
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(NOTES_DIR.glob("*.txt"))
    return [
        {
            "uri": f"notes://{f.stem}",
            "name": f.stem,
            "description": f"Note: {f.stem}",
            "mimeType": "text/plain",
        }
        for f in files
    ]


def read_resource(uri: str) -> tuple[str, bool]:
    """Read a note resource by URI. Returns (content, is_error)."""
    if not uri.startswith("notes://"):
        return f"Unsupported URI scheme: '{uri}' (expected notes://title)", True
    title = uri[len("notes://"):]
    if not title:
        return "Empty note title in URI", True
    path = _note_path(title)
    if not path.exists():
        return f"Note '{title}' not found. Use list_notes to see available notes.", True
    return path.read_text(encoding="utf-8"), False


# ── Prompt templates (returned by prompts/list) ───────────────────────────────
# Prompt templates let this server inject canned LLM instructions with dynamic
# content. The client calls prompts/get with the template name and arguments;
# we return a list of messages that the client injects into the conversation.

PROMPTS = [
    {
        "name": "summarize_notes",
        "description": "Ask the LLM to summarize all your saved notes in a structured way.",
        "arguments": [],
    },
    {
        "name": "find_related",
        "description": "Ask the LLM to find notes related to a given topic.",
        "arguments": [
            {
                "name": "topic",
                "description": "Topic or keyword to search for across notes",
                "required": True,
            }
        ],
    },
    {
        "name": "draft_note",
        "description": "Ask the LLM to draft a new note on a subject, optionally with extra context.",
        "arguments": [
            {"name": "title", "description": "Title for the new note", "required": True},
            {"name": "context", "description": "Background context or key points to include", "required": False},
        ],
    },
]


def get_prompt(name: str, arguments: dict) -> tuple[list[dict], bool]:
    """Build MCP messages for a prompt template. Returns (messages, is_error)."""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    if name == "summarize_notes":
        files = sorted(NOTES_DIR.glob("*.txt"))
        if not files:
            note_list = "(no notes saved yet)"
        else:
            note_list = "\n".join(
                f"- {f.stem}: {f.read_text(encoding='utf-8')[:120].strip()}..."
                if len(f.read_text(encoding="utf-8")) > 120
                else f"- {f.stem}: {f.read_text(encoding='utf-8').strip()}"
                for f in files
            )
        text = (
            f"Here are the user's saved notes:\n\n{note_list}\n\n"
            "Please summarize these notes in a clear, structured way. "
            "Group related ideas together and highlight any key action items or insights."
        )
        return [{"role": "user", "content": {"type": "text", "text": text}}], False

    elif name == "find_related":
        topic = arguments.get("topic", "").strip()
        if not topic:
            return [], True  # missing required arg — caller returns an error
        files = sorted(NOTES_DIR.glob("*.txt"))
        if not files:
            note_list = "(no notes saved yet)"
        else:
            note_list = "\n\n".join(
                f"### {f.stem}\n{f.read_text(encoding='utf-8').strip()}" for f in files
            )
        text = (
            f"Topic: **{topic}**\n\n"
            f"The user's saved notes:\n\n{note_list}\n\n"
            f"List only the notes that are directly relevant to '{topic}'. "
            "For each relevant note, briefly explain the connection. "
            "Skip notes that are not related."
        )
        return [{"role": "user", "content": {"type": "text", "text": text}}], False

    elif name == "draft_note":
        title = arguments.get("title", "").strip()
        if not title:
            return [], True
        context = arguments.get("context", "").strip()
        context_line = f"\n\nContext / key points to include:\n{context}" if context else ""
        text = (
            f"Please draft a concise, well-structured note titled '{title}'.{context_line}\n\n"
            "Write it in plain text, suitable for saving as a personal note. "
            "Keep it focused and useful."
        )
        return [{"role": "user", "content": {"type": "text", "text": text}}], False

    else:
        return [], True  # unknown prompt name


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
                "capabilities": CAPABILITIES,
                "serverInfo": SERVER_INFO,
            })

        elif method == "notifications/initialized":
            pass  # notification — no response expected

        elif method == "tools/list":
            _respond(req_id, {"tools": TOOLS})

        elif method == "resources/list":
            _respond(req_id, {"resources": list_resources()})

        elif method == "resources/read":
            params = req.get("params", {})
            uri = params.get("uri", "")
            content, is_error = read_resource(uri)
            if is_error:
                _error(req_id, -32602, content)
            else:
                _respond(req_id, {
                    "contents": [{"uri": uri, "mimeType": "text/plain", "text": content}]
                })

        elif method == "prompts/list":
            _respond(req_id, {"prompts": PROMPTS})

        elif method == "prompts/get":
            params = req.get("params", {})
            prompt_name = params.get("name", "")
            arguments = params.get("arguments", {}) or {}
            messages, is_error = get_prompt(prompt_name, arguments)
            if is_error:
                _error(req_id, -32602, f"Unknown prompt or missing required argument: '{prompt_name}'")
            else:
                _respond(req_id, {
                    "description": next((p["description"] for p in PROMPTS if p["name"] == prompt_name), ""),
                    "messages": messages,
                })

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
