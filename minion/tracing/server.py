"""Trace viewer HTTP server — serves ui.html and /api/events.

Usage (from cli.py):
    from minion.tracing.server import start_viewer
    start_viewer(session_id="abc-123", traces_dir=Path(...), port=7331)

Only stdlib used: http.server, json, webbrowser, pathlib.
"""

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

_UI_PATH = Path(__file__).parent / "ui.html"


def start_viewer(
    session_id: str,
    traces_dir: Path,
    port: int = 7331,
) -> None:
    """Start the trace viewer, open the browser, block until Ctrl+C."""
    jsonl_path = traces_dir / f"{session_id}.jsonl"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/" or path == "":
                self._serve_ui()
            elif path == "/api/events":
                self._serve_events()
            else:
                self.send_error(404)

        def _serve_ui(self) -> None:
            try:
                data = _UI_PATH.read_bytes()
            except OSError as e:
                self.send_error(500, str(e))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_events(self) -> None:
            events = []
            if jsonl_path.exists():
                try:
                    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line:
                            try:
                                events.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                except OSError:
                    pass

            body = json.dumps(events).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # noqa: ARG002
            pass  # silence access log

    server = HTTPServer(("localhost", port), Handler)
    url = f"http://localhost:{port}"
    print(f"  Trace viewer: {url}  (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
