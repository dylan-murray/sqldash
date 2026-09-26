"""Shared test helpers for driving the HTTP API.

Both the single-project and workspace suites poll `/api/executions`; keeping
one implementation means a fix like the cold-start warm-up lands everywhere
instead of in whichever module noticed first.
"""

import base64
import contextlib
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TERMINAL = ("done", "error", "cancelled")


def run_to_completion(client, payload, timeout=30.0):
    """Submit a run and poll until it reaches a terminal status."""
    res = client.post("/api/run", json=payload)
    assert res.status_code == 202, res.text
    execution_id = res.json()["id"]
    ex = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        ex = client.get(f"/api/executions/{execution_id}").json()
        if ex["status"] in TERMINAL:
            return execution_id, ex
        time.sleep(0.05)
    status = ex["status"] if ex else "unknown"
    raise AssertionError(f"execution did not finish within {timeout}s (last status: {status})")


def warm_up(client, dashboard):
    """Pay duckdb's first-use cost (extension load, CSV attach) outside any
    per-test deadline. On a cold environment — the first run after
    `uv sync` — that first query alone can exceed a 10s budget, which made
    whichever test touched the warehouse first fail while warm re-runs passed.

    Note the terminal check: `queued` is a real status (submit() registers the
    state before the pool picks it up), so waiting for "not running" would
    return immediately and warm nothing.
    """
    res = client.post("/api/run", json={"dashboard": dashboard, "sql": "SELECT 1"})
    if res.status_code != 202:
        return
    execution_id = res.json()["id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        if client.get(f"/api/executions/{execution_id}").json()["status"] in TERMINAL:
            return
        time.sleep(0.05)


@contextlib.contextmanager
def git_http_server(project_root: Path, user: str, password: str):
    """Serve bare repos under project_root over smart HTTP behind basic auth.

    `git clone --depth 1` needs the smart protocol, so requests go through
    `git http-backend` as CGI. Yields the base URL without credentials."""
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _serve(self):
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            env = {
                **os.environ,
                "GIT_PROJECT_ROOT": str(project_root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": self.command,
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                "REMOTE_USER": user,
            }
            out = subprocess.run(
                ["git", "http-backend"], input=body, env=env, capture_output=True
            ).stdout
            head, _, payload = out.partition(b"\r\n\r\n")
            headers = [line.split(":", 1) for line in head.decode().split("\r\n") if line]
            status = next((v.strip() for k, v in headers if k.lower() == "status"), "200")
            self.send_response(int(status.split()[0]))
            for key, value in headers:
                if key.lower() != "status":
                    self.send_header(key, value.strip())
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _serve
        do_POST = _serve

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
