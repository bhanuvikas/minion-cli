"""Minimal standalone A2A echo agent for integration testing.

Zero dependencies outside stdlib. Implements the same 4 A2A endpoints that
minion's A2A server provides, returning an echo artifact for each task.

Start with:
    python examples/a2a_echo_agent.py [--port 8181]

Then configure in .minion/a2a.json:
    {
      "agents": {
        "echo": { "url": "http://localhost:8181" }
      }
    }

The agent waits 0.1s before completing to simulate real processing time.
This makes it useful for testing parallel task dispatch and the SSE stream.
"""

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_tasks: dict = {}
_task_events: dict = {}
_lock = threading.Lock()

AGENT_CARD = {
    "name": "echo",
    "description": "Minimal A2A echo agent for integration testing. Returns 'Echo: <input>' for any task.",
    "url": "",  # filled in at startup
    "version": "0.1.0",
    "capabilities": {"streaming": True},
    "skills": [
        {"id": "echo", "name": "Echo", "description": "Echoes the input task back as output."}
    ],
}


class EchoHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/.well-known/agent.json":
            self._json(200, AGENT_CARD)
        elif self.path.startswith("/tasks/"):
            task_id = self.path[7:].strip("/")
            with _lock:
                task = _tasks.get(task_id)
            if task is None:
                self._json(404, {"error": "not found"})
            else:
                self._json(200, task)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}

        if self.path == "/tasks/send":
            self._handle_send(body)
        elif self.path == "/tasks/sendSubscribe":
            self._handle_subscribe(body)
        else:
            self._json(404, {"error": "not found"})

    def _handle_send(self, body):
        message = body.get("message", "")
        task_id = str(uuid.uuid4())
        done = threading.Event()
        with _lock:
            _tasks[task_id] = {"id": task_id, "status": "submitted",
                               "input": {"message": message}}
            _task_events[task_id] = done
        threading.Thread(target=self._run, args=(task_id, message, done), daemon=True).start()
        self._json(202, {"id": task_id, "status": "submitted"})

    def _handle_subscribe(self, body):
        message = body.get("message", "")
        task_id = str(uuid.uuid4())
        done = threading.Event()
        with _lock:
            _tasks[task_id] = {"id": task_id, "status": "submitted",
                               "input": {"message": message}}
            _task_events[task_id] = done

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        self._sse({"id": task_id, "status": "submitted"})
        self._sse({"id": task_id, "status": "working"})

        threading.Thread(target=self._run, args=(task_id, message, done), daemon=True).start()
        done.wait(timeout=30)

        with _lock:
            task = _tasks.get(task_id, {"status": "failed", "error": "timeout"})
        self._sse(task)

    @staticmethod
    def _run(task_id: str, message: str, done: threading.Event):
        time.sleep(0.1)  # simulate processing
        with _lock:
            _tasks[task_id] = {
                "id": task_id,
                "status": "completed",
                "input": {"message": message},
                "artifacts": [{"text": f"Echo: {message}"}],
            }
        done.set()

    def _sse(self, payload: dict):
        try:
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet


def main():
    import argparse
    parser = argparse.ArgumentParser(description="A2A echo agent for minion-cli testing")
    parser.add_argument("--port", type=int, default=8181)
    parser.add_argument("--host", default="localhost")
    args = parser.parse_args()

    AGENT_CARD["url"] = f"http://{args.host}:{args.port}"
    server = ThreadingHTTPServer((args.host, args.port), EchoHandler)
    print(f"A2A echo agent at http://{args.host}:{args.port}")
    print(f"  /.well-known/agent.json  →  Agent Card")
    print(f"  POST /tasks/send         →  Submit task (polling)")
    print(f"  POST /tasks/sendSubscribe →  Submit task (SSE)")
    print(f"  GET  /tasks/{{id}}         →  Poll task status")
    print(f"  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
