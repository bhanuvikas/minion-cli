"""MCPHTTPClient — Streamable HTTP transport for MCP.

Implements MCPClientBase using the MCP Streamable HTTP transport spec:
  - All client→server messages go via HTTP POST to a single /mcp endpoint
  - Server→client notifications come via a persistent GET SSE stream
  - Session lifecycle is tracked with the Mcp-Session-Id header

Transport overview:
  POST /mcp
    Request body:  JSON-RPC request or notification (Content-Type: application/json)
    Accept header: application/json, text/event-stream
    Response:
      - Content-Type: application/json  → single JSON-RPC response
      - Content-Type: text/event-stream → SSE stream (for streaming tool results)

  GET /mcp
    Accept: text/event-stream
    Persistent SSE stream for server-initiated notifications (no id) and
    optionally server-pushed responses. Client reconnects with Last-Event-ID
    so the server can replay missed events.

  DELETE /mcp
    Mcp-Session-Id: <id>
    Signals session cleanup. Best-effort — failure is ignored.

Session management:
  The server assigns a session ID in the Mcp-Session-Id response header on
  the initialize POST. All subsequent requests echo this header so the server
  can associate requests with the session.

Race safety:
  _send_request() is synchronous (one call at a time per client). The GET
  stream thread only dispatches notifications — it never delivers request
  responses, so there is no per-request queue needed for HTTP transport.
"""

from __future__ import annotations

import http.client
import json
import threading
from typing import Optional
from urllib.parse import urlparse

from .base import MCPClientBase
from .config import MCPServerConfig
from .sse import SSEParser


class MCPHTTPClient(MCPClientBase):
    """MCP client over Streamable HTTP transport.

    Lifecycle:
        client = MCPHTTPClient("workspace", config)
        client.notification_callback = some_fn   # optional
        client.connect()
        result = client.call_tool("list_files", {"directory": "."})
        client.shutdown()
    """

    def __init__(self, name: str, config: MCPServerConfig) -> None:
        super().__init__(name, config)
        self._host: str = ""
        self._port: int = 80
        self._path: str = "/mcp"
        self._scheme: str = "http"
        self._session_id: Optional[str] = None
        self._last_event_id: Optional[str] = None
        self._get_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()

    # ── MCPClientBase abstract methods ────────────────────────────────────────

    def _initialize_transport(self) -> None:
        """Parse and validate the configured URL. No network activity yet."""
        parsed = urlparse(self.config.url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise RuntimeError(
                f"MCP HTTP server '{self.name}' has unsupported URL scheme '{scheme}': "
                f"{self.config.url!r} — use http:// or https://"
            )
        if not parsed.hostname:
            raise RuntimeError(
                f"MCP HTTP server '{self.name}' has no hostname in URL: {self.config.url!r}"
            )
        self._scheme = scheme
        self._host = parsed.hostname
        self._port = parsed.port or (443 if scheme == "https" else 80)
        self._path = parsed.path or "/mcp"

    def _after_handshake(self) -> None:
        """Open the persistent GET SSE stream for server-initiated notifications."""
        self._get_thread = threading.Thread(target=self._get_stream_loop, daemon=True)
        self._get_thread.name = f"mcp-http-stream-{self.name}"
        self._get_thread.start()

    def _send_request(self, method: str, params: dict | None = None) -> dict:
        """POST a JSON-RPC request and return the parsed response.

        Handles both direct JSON responses and SSE-wrapped responses transparently.
        Raises IOError on connection failure, RuntimeError on server error.
        """
        req_id = self._next_id
        self._next_id += 1
        payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params

        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Content-Length": str(len(body)),
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        conn = self._make_connection()
        try:
            conn.request("POST", self._path, body=body, headers=headers)
            resp = conn.getresponse()
            self._extract_session_id(resp)

            content_type = resp.getheader("Content-Type", "")
            if "text/event-stream" in content_type:
                return self._read_sse_response(resp, req_id)
            else:
                data = resp.read()
                return json.loads(data.decode())
        except (http.client.HTTPException, OSError) as e:
            raise IOError(f"HTTP transport error for '{self.name}': {e}") from e
        finally:
            conn.close()

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """POST a JSON-RPC notification (no id, no response expected)."""
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params

        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        conn = self._make_connection()
        try:
            conn.request("POST", self._path, body=body, headers=headers)
            resp = conn.getresponse()
            resp.read()  # consume body to allow connection reuse
        except Exception:
            pass  # notifications are fire-and-forget
        finally:
            conn.close()

    def shutdown(self) -> None:
        """Signal the GET stream thread to stop and send a DELETE to clean up the session."""
        self._shutdown_event.set()
        if self._session_id:
            try:
                conn = self._make_connection()
                conn.request("DELETE", self._path, headers={"Mcp-Session-Id": self._session_id})
                resp = conn.getresponse()
                resp.read()
                conn.close()
            except Exception:
                pass
        if self._get_thread is not None:
            self._get_thread.join(timeout=2)
            self._get_thread = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_connection(self) -> http.client.HTTPConnection:
        """Create a new HTTP(S) connection. Caller is responsible for closing it."""
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=30)
        return http.client.HTTPConnection(self._host, self._port, timeout=30)

    def _extract_session_id(self, resp: http.client.HTTPResponse) -> None:
        """Capture Mcp-Session-Id from the first server response that carries it."""
        if self._session_id is None:
            sid = resp.getheader("Mcp-Session-Id")
            if sid:
                self._session_id = sid

    def _read_sse_response(self, resp: http.client.HTTPResponse, req_id: int) -> dict:
        """Read an SSE stream looking for the response matching req_id.

        Notifications encountered while scanning (no id) are dispatched inline.
        Raises IOError if the stream ends before a matching response is found.
        """
        for event in SSEParser.iter_events(resp):
            if event.id:
                self._last_event_id = event.id
            try:
                parsed = json.loads(event.data)
            except json.JSONDecodeError:
                continue

            msg_id = parsed.get("id")
            if msg_id == req_id:
                return parsed
            if msg_id is None:
                # Server-initiated notification embedded in POST response stream
                self._handle_notification(parsed)

        raise IOError(
            f"SSE stream for '{self.name}' ended before response to request {req_id} arrived"
        )

    # ── Background GET stream thread ──────────────────────────────────────────

    def _get_stream_loop(self) -> None:
        """Daemon thread: open GET /mcp and dispatch server-initiated notifications.

        Uses Last-Event-ID on reconnect so the server can replay any events
        missed during a temporary disconnection.

        Exits cleanly on shutdown signal or if the server does not support GET
        streams (non-200 status).
        """
        while not self._shutdown_event.is_set():
            try:
                conn = self._make_connection()
                headers: dict[str, str] = {
                    "Accept": "text/event-stream",
                    "Cache-Control": "no-cache",
                }
                if self._session_id:
                    headers["Mcp-Session-Id"] = self._session_id
                if self._last_event_id:
                    headers["Last-Event-ID"] = self._last_event_id

                conn.request("GET", self._path, headers=headers)
                resp = conn.getresponse()

                if resp.status != 200:
                    resp.read()
                    conn.close()
                    break  # Server doesn't support GET stream — exit gracefully

                for event in SSEParser.iter_events(resp):
                    if self._shutdown_event.is_set():
                        break
                    if event.id:
                        self._last_event_id = event.id
                    try:
                        parsed = json.loads(event.data)
                    except json.JSONDecodeError:
                        continue
                    if parsed.get("id") is None:
                        self._handle_notification(parsed)

                conn.close()
            except Exception:
                if self._shutdown_event.is_set():
                    break
                # Network hiccup — exit rather than spin-loop; reconnect is not worth
                # the complexity for a learning implementation.
                break
