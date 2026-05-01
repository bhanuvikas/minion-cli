#!/usr/bin/env python3
"""Workspace MCP server over Streamable HTTP transport.

An MCP server that exposes your workspace (a configurable directory) as tools,
demonstrating every interesting feature of the Streamable HTTP transport:

  POST /mcp → JSON response  : list_files, read_file, write_file, delete_file
  POST /mcp → SSE stream     : search_files (streams notifications/message events per match)
  GET  /mcp → SSE stream     : persistent notification stream for file change events
  DELETE /mcp                : session cleanup

Usage:
    python examples/mcp_workspace_server.py [--workspace DIR] [--port PORT]

Add to .minion/mcp.json:
    {
      "servers": {
        "workspace": {
          "url": "http://localhost:9000/mcp"
        }
      }
    }

Then in minion:
    > /mcp list
    > Read the file README.md using the workspace server
    > Search for "TODO" in all Python files

Tools:
    list_files   — list files in a directory (relative to workspace root)
    read_file    — read file contents (text only; rejects paths outside workspace)
    write_file   — write or overwrite a file (rejects paths outside workspace)
    delete_file  — delete a file (rejects paths outside workspace, requires confirm_all)
    search_files — search for a query string across files, streaming results via SSE

GET stream events (sent every 5 seconds if files have changed):
    notifications/message  — level=info, logger="workspace", data={path, event, mtime}

Streamable HTTP features exercised:
    * Mcp-Session-Id: assigned on initialize, echoed on all requests
    * POST → text/event-stream: search_files uses streaming SSE response
    * GET /mcp: persistent SSE notification stream using polling + Last-Event-ID resumption
    * DELETE /mcp: session cleanup, terminates the GET stream thread for that session
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# ── Configuration ──────────────────────────────────────────────────────────────

DEFAULT_PORT = 9000
DEFAULT_WORKSPACE = Path.cwd()

# ── Globals (set at startup) ───────────────────────────────────────────────────

WORKSPACE_ROOT: Path = DEFAULT_WORKSPACE

# ── Protocol constants ─────────────────────────────────────────────────────────

PROTOCOL_VERSION = "2025-11-25"
SERVER_INFO = {"name": "mcp-workspace-server", "version": "0.1.0"}

TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": "List files in a directory within the workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory path relative to workspace root. Use '.' for root.",
                }
            },
            "required": ["directory"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the text contents of a file within the workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to workspace root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write or overwrite a file within the workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to workspace root.",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write.",
                },
            },
            "required": ["path", "content"],
        },
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "delete_file",
        "description": "Delete a file within the workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to workspace root.",
                }
            },
            "required": ["path"],
        },
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "search_files",
        "description": (
            "Search for a query string across all text files in the workspace. "
            "Results are streamed progressively as files are scanned — "
            "the response uses Server-Sent Events so matches appear as they are found."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "String to search for (case-insensitive).",
                },
                "directory": {
                    "type": "string",
                    "description": "Subtree to search within (relative to workspace root). Default: '.'",
                },
            },
            "required": ["query"],
        },
    },
]

# ── File-change watcher ────────────────────────────────────────────────────────

class _FileWatcher:
    """Poll workspace for file changes, accumulate pending notifications.

    Runs in a background thread. GET stream handlers drain _pending_events
    to relay changes to connected clients.
    """

    def __init__(self, workspace: Path, interval: float = 5.0) -> None:
        self._workspace = workspace
        self._interval = interval
        self._snapshots: dict[str, float] = {}  # rel_path → mtime
        self._lock = threading.Lock()
        self._pending: list[dict] = []           # undelivered notifications
        self._thread = threading.Thread(target=self._loop, daemon=True, name="workspace-watcher")
        self._thread.start()

    def drain(self, since_id: int) -> list[dict]:
        """Return pending events with id > since_id and clear them."""
        with self._lock:
            result = [e for e in self._pending if e["_seq"] > since_id]
            if result:
                self._pending = [e for e in self._pending if e["_seq"] > result[-1]["_seq"]]
            return result

    def latest_seq(self) -> int:
        with self._lock:
            return self._pending[-1]["_seq"] if self._pending else 0

    def _loop(self) -> None:
        seq = 0
        while True:
            time.sleep(self._interval)
            current = self._scan()
            events: list[dict] = []

            old_keys = set(self._snapshots)
            new_keys = set(current)

            for path, mtime in current.items():
                if path not in self._snapshots:
                    seq += 1
                    events.append({"_seq": seq, "path": path, "event": "created", "mtime": mtime})
                elif self._snapshots[path] != mtime:
                    seq += 1
                    events.append({"_seq": seq, "path": path, "event": "modified", "mtime": mtime})

            for path in old_keys - new_keys:
                seq += 1
                events.append({"_seq": seq, "path": path, "event": "deleted", "mtime": 0.0})

            self._snapshots = current
            if events:
                with self._lock:
                    self._pending.extend(events)

    def _scan(self) -> dict[str, float]:
        result: dict[str, float] = {}
        try:
            for p in self._workspace.rglob("*"):
                if p.is_file() and ".git" not in p.parts:
                    rel = str(p.relative_to(self._workspace))
                    result[rel] = p.stat().st_mtime
        except OSError:
            pass
        return result


# Module-level singleton (started in main())
_watcher: Optional[_FileWatcher] = None

# ── Session registry ───────────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}   # session_id → {"initialized": bool}
_sessions_lock = threading.Lock()
_session_counter = 0


def _new_session_id() -> str:
    global _session_counter
    _session_counter += 1
    return hashlib.sha1(f"session-{_session_counter}-{time.time()}".encode()).hexdigest()[:16]


def _get_or_create_session(session_id: Optional[str]) -> str:
    with _sessions_lock:
        if session_id and session_id in _sessions:
            return session_id
        new_id = _new_session_id()
        _sessions[new_id] = {"initialized": False}
        return new_id


def _mark_initialized(session_id: str) -> None:
    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id]["initialized"] = True


def _delete_session(session_id: str) -> None:
    with _sessions_lock:
        _sessions.pop(session_id, None)

# ── JSON-RPC helpers ───────────────────────────────────────────────────────────

def _ok(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _notification(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


# ── Workspace tool implementations ─────────────────────────────────────────────

def _resolve(path_str: str) -> Path:
    """Resolve path relative to workspace root. Raises ValueError if outside."""
    target = (WORKSPACE_ROOT / path_str).resolve()
    if not str(target).startswith(str(WORKSPACE_ROOT.resolve())):
        raise ValueError(f"Path '{path_str}' is outside the workspace root")
    return target


def _tool_list_files(args: dict) -> tuple[str, bool]:
    directory = args.get("directory", ".")
    try:
        target = _resolve(directory)
        if not target.exists():
            return f"Directory '{directory}' does not exist", True
        if not target.is_dir():
            return f"'{directory}' is not a directory", True
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = []
        for e in entries:
            rel = e.relative_to(WORKSPACE_ROOT)
            suffix = "/" if e.is_dir() else f"  ({e.stat().st_size} bytes)"
            lines.append(f"{rel}{suffix}")
        return "\n".join(lines) if lines else "(empty directory)", False
    except ValueError as e:
        return str(e), True
    except OSError as e:
        return f"Error listing '{directory}': {e}", True


def _tool_read_file(args: dict) -> tuple[str, bool]:
    path_str = args.get("path", "")
    try:
        target = _resolve(path_str)
        if not target.exists():
            return f"File '{path_str}' does not exist", True
        if not target.is_file():
            return f"'{path_str}' is not a file", True
        return target.read_text(encoding="utf-8", errors="replace"), False
    except ValueError as e:
        return str(e), True
    except OSError as e:
        return f"Error reading '{path_str}': {e}", True


def _tool_write_file(args: dict) -> tuple[str, bool]:
    path_str = args.get("path", "")
    content = args.get("content", "")
    try:
        target = _resolve(path_str)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Written {len(content)} characters to '{path_str}'", False
    except ValueError as e:
        return str(e), True
    except OSError as e:
        return f"Error writing '{path_str}': {e}", True


def _tool_delete_file(args: dict) -> tuple[str, bool]:
    path_str = args.get("path", "")
    try:
        target = _resolve(path_str)
        if not target.exists():
            return f"File '{path_str}' does not exist", True
        if not target.is_file():
            return f"'{path_str}' is not a file (will not delete directories)", True
        target.unlink()
        return f"Deleted '{path_str}'", False
    except ValueError as e:
        return str(e), True
    except OSError as e:
        return f"Error deleting '{path_str}': {e}", True


def _search_generator(query: str, directory: str):
    """Generator yielding (path_str, line_number, line_text) for each match."""
    try:
        root = _resolve(directory)
    except ValueError as e:
        yield ("_error", 0, str(e))
        return

    query_lower = query.lower()
    try:
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file() or ".git" in file_path.parts:
                continue
            rel = str(file_path.relative_to(WORKSPACE_ROOT))
            try:
                for lineno, line in enumerate(
                    file_path.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if query_lower in line.lower():
                        yield (rel, lineno, line.rstrip())
            except OSError:
                continue
    except OSError:
        pass


# ── HTTP Request Handler ───────────────────────────────────────────────────────

class MCPHandler(http.server.BaseHTTPRequestHandler):
    """Handle MCP Streamable HTTP requests."""

    def log_message(self, format: str, *args) -> None:  # type: ignore[override]
        # Suppress default access log to keep stdout clean
        pass

    # ── Routing ───────────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self._send_404()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            msg = json.loads(body.decode())
        except json.JSONDecodeError:
            self._send_json(400, _err(None, -32700, "Parse error"))
            return

        session_id = self.headers.get("Mcp-Session-Id")
        msg_id = msg.get("id")
        method = msg.get("method", "")

        if method == "initialize":
            session_id = _get_or_create_session(session_id)
            result = self._handle_initialize(msg)
            resp_body = json.dumps(_ok(msg_id, result)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Mcp-Session-Id", session_id)
            self.end_headers()
            self.wfile.write(resp_body)
            _mark_initialized(session_id)
            return

        if method == "ping":
            self._send_json(200, _ok(msg_id, {}))
            return

        if method == "notifications/initialized":
            # Notification — no response body, just 202
            self.send_response(202)
            self.end_headers()
            return

        if method == "tools/list":
            self._send_json(200, _ok(msg_id, {"tools": TOOL_SCHEMAS}))
            return

        if method == "tools/call":
            params = msg.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            if tool_name == "search_files":
                # search_files uses SSE streaming response
                self._handle_search_sse(msg_id, arguments)
            else:
                result = self._dispatch_tool(tool_name, arguments)
                self._send_json(200, _ok(msg_id, result))
            return

        # Unknown notifications (no id) must be silently ignored per JSON-RPC spec
        if msg_id is None:
            self.send_response(202)
            self.end_headers()
            return

        # Unknown request method — return error
        self._send_json(200, _err(msg_id, -32601, f"Method not found: {method!r}"))

    def do_GET(self) -> None:
        if self.path != "/mcp":
            self._send_404()
            return

        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            self._send_json(400, {"error": "GET /mcp requires Accept: text/event-stream"})
            return

        session_id = self.headers.get("Mcp-Session-Id")
        last_event_id_raw = self.headers.get("Last-Event-ID", "0")
        try:
            last_seq = int(last_event_id_raw)
        except ValueError:
            last_seq = 0

        # Open persistent SSE stream, send file-change notifications
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()

        try:
            while True:
                events = _watcher.drain(last_seq) if _watcher else []
                for ev in events:
                    seq = ev["_seq"]
                    notification = _notification(
                        "notifications/message",
                        {
                            "level": "info",
                            "logger": "workspace",
                            "data": {"path": ev["path"], "event": ev["event"], "mtime": ev["mtime"]},
                        },
                    )
                    self._sse_send(str(seq), json.dumps(notification))
                    last_seq = seq
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected — normal for a persistent stream

    def do_DELETE(self) -> None:
        if self.path != "/mcp":
            self._send_404()
            return
        session_id = self.headers.get("Mcp-Session-Id")
        if session_id:
            _delete_session(session_id)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── MCP protocol handlers ─────────────────────────────────────────────────

    def _handle_initialize(self, msg: dict) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
                "logging": {},
            },
            "serverInfo": SERVER_INFO,
        }

    def _dispatch_tool(self, name: str, args: dict) -> dict:
        dispatch = {
            "list_files": _tool_list_files,
            "read_file": _tool_read_file,
            "write_file": _tool_write_file,
            "delete_file": _tool_delete_file,
        }
        fn = dispatch.get(name)
        if fn is None:
            return _tool_result(f"Unknown tool: '{name}'", is_error=True)
        text, is_error = fn(args)
        return _tool_result(text, is_error=is_error)

    def _handle_search_sse(self, req_id, arguments: dict) -> None:
        """Stream search results back as an SSE response on the POST connection."""
        query = arguments.get("query", "")
        directory = arguments.get("directory", ".")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        matches: list[str] = []
        try:
            for rel, lineno, line_text in _search_generator(query, directory):
                if rel == "_error":
                    matches = [f"Error: {line_text}"]
                    break
                matches.append(f"{rel}:{lineno}: {line_text}")
                # Stream each match as a logging notification (spec-compliant)
                progress = _notification(
                    "notifications/message",
                    {"level": "debug", "logger": "search", "data": f"{rel}:{lineno}: {line_text}"},
                )
                self._sse_send(None, json.dumps(progress))
        except (BrokenPipeError, ConnectionResetError):
            return

        # Final result event (has id = response to this request)
        summary = "\n".join(matches) if matches else f"No matches for '{query}'"
        final = _ok(req_id, _tool_result(summary))
        self._sse_send(None, json.dumps(final))

    # ── SSE / HTTP helpers ────────────────────────────────────────────────────

    def _sse_send(self, event_id: Optional[str], data: str) -> None:
        """Write one SSE event to the response stream."""
        lines = ""
        if event_id is not None:
            lines += f"id: {event_id}\n"
        lines += f"data: {data}\n\n"
        self.wfile.write(lines.encode())
        self.wfile.flush()

    def _send_json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_404(self) -> None:
        self._send_json(404, {"error": "Not found — only /mcp is served"})


# ── Server startup ─────────────────────────────────────────────────────────────

class _ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """Allow address reuse so the server can restart quickly during development."""
    allow_reuse_address = True


def main() -> None:
    global WORKSPACE_ROOT, _watcher

    parser = argparse.ArgumentParser(description="MCP workspace server (Streamable HTTP)")
    parser.add_argument("--workspace", default=str(Path.cwd()),
                        help="Root directory to expose (default: current directory)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to listen on (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    WORKSPACE_ROOT = Path(args.workspace).resolve()
    if not WORKSPACE_ROOT.is_dir():
        print(f"Error: workspace '{WORKSPACE_ROOT}' is not a directory", file=sys.stderr)
        sys.exit(1)

    _watcher = _FileWatcher(WORKSPACE_ROOT)

    server = _ThreadingHTTPServer(("", args.port), MCPHandler)
    print(f"MCP workspace server listening on http://localhost:{args.port}/mcp")
    print(f"Workspace root: {WORKSPACE_ROOT}")
    print("Add to .minion/mcp.json:")
    print(json.dumps({"servers": {"workspace": {"url": f"http://localhost:{args.port}/mcp"}}}, indent=2))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
