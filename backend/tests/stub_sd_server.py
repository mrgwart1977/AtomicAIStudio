"""Stub sd-server for M4.1 Phase 2A tests.

Speaks just enough of the real surface to exercise the adapter,
protocol, pool, and supervisor end-to-end — with zero inference:

  GET  /v1/models                → 200 {"data":[...]} (readiness/health)
  POST /v1/images/generations    → per STUB_MODE
  GET  /v1/child_pid             → spawned child pid (tree-kill test)

Launch args it understands: -l/--listen-ip, --listen-port, --model-dir,
--log-level (extra args ignored) — same vector the real adapter passes.

Env switches:
  STUB_MODE        ok | error | malformed | hang | drop | a1111 | crash | noimg | nomodel
                   ok     → documented shape for the POSTed path:
                            /sdapi/v1/txt2img → {"images":[b64]},
                            /v1/images/generations → {"data":[{b64_json}]}
                   a1111  → force A1111-style {"images":[b64]} on any path
                   crash  → os._exit(2) mid-request (recovery tests)
                   noimg  → 200 {} — valid JSON without image payload
                   nomodel → 503 {"error":"No model is loaded","code":"model_not_loaded"}
                            (real server's no-weights response)
  STUB_MODELS_CODE HTTP status for /v1/models (default 200)
  STUB_MODELS_FAIL_AFTER  return 500 after N successful /v1/models calls
  STUB_BIND_PORT   fixed bind port (collision tests); bind failure → exit 3
  STUB_CHILD       spawn a sleeping child process (tree-kill test)
  STUB_NOISY       emit a log line per poll (stdout-drain exercise)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 1x1 opaque PNG — real bytes so the adapter's write+sha path runs.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _log(msg: str) -> None:
    sys.stdout.write(f"[stub-sd-server] {msg}\n")
    sys.stdout.flush()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence default stderr logging
        pass

    def _send(self, code: int, body) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if _env("STUB_NOISY"):
            _log(f"GET {self.path}")
        if self.path == "/v1/models":
            self.server.models_hits = getattr(self.server, "models_hits", 0) + 1
            fail_after = int(_env("STUB_MODELS_FAIL_AFTER", "0") or 0)
            if fail_after and self.server.models_hits > fail_after:
                code = 500
            else:
                code = int(_env("STUB_MODELS_CODE", "200") or 200)
            self._send(code, {"object": "list",
                              "data": [{"id": "stub-model"}] if code == 200 else []})
        elif self.path == "/v1/child_pid":
            self._send(200, {"child_pid": self.server.child_pid})
        elif self.path == "/v1/last_request":
            # Phase 2B: lets tests assert the exact request contract the
            # adapter sends (the "unverified superset" made verifiable).
            self._send(200, self.server.last_request or {})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)  # consume the request body
        try:
            self.server.last_request = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self.server.last_request = {"_raw": body[:200].decode("utf-8", "replace")}
        mode = _env("STUB_MODE", "ok")
        _log(f"POST {self.path} mode={mode}")
        if self.path not in ("/v1/images/generations", "/sdapi/v1/txt2img"):
            self._send(404, {"error": "not found"})
            return
        if mode == "error":
            self._send(500, {"error": "generation exploded"})
        elif mode == "malformed":
            self._send(200, b"<html>this is not json at all</html>")
        elif mode == "hang":
            time.sleep(999)
        elif mode == "drop":
            # Close the connection mid-request without a response —
            # transports see a clean disconnect, server-side work is
            # ambiguous → the worker must be recycled, not reused.
            self.connection.close()
            self.close_connection = True
            return
        elif mode == "crash":
            # Die mid-request — transport sees a dropped connection AND
            # the process is gone. Exercises worker-recovery paths.
            os._exit(2)
        elif mode == "noimg":
            self._send(200, {"created": int(time.time()), "data": []})
        elif mode == "nomodel":
            # Real server's no-weights response (api.md: generation
            # endpoints return 503 model_not_loaded when unloaded).
            self._send(503, {"error": "No model is loaded",
                             "code": "model_not_loaded"})
        elif mode == "a1111" or (mode == "ok" and self.path == "/sdapi/v1/txt2img"):
            # A1111-style txt2img response shape — the documented shape
            # for the sdapi endpoint the adapter now uses.
            self._send(200, {"images": [PNG_B64],
                             "parameters": {}, "info": ""})
        else:  # ok — OpenAI shape for /v1/images/generations
            self._send(200, {
                "created": int(time.time()),
                "data": [{"b64_json": PNG_B64, "url": ""}],
            })


def main() -> None:
    port = 1234
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] in ("--listen-port", "--port") and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1]); i += 1
        i += 1
    port = int(_env("STUB_BIND_PORT", str(port)) or port)

    child = None
    if _env("STUB_CHILD"):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"])

    _log(f"listening on 127.0.0.1:{port} (no model weights loaded)")
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        _log(f"FATAL: cannot bind port {port}: {e}")
        sys.exit(3)
    srv.child_pid = child.pid if child else None

    def noisy():
        while _env("STUB_NOISY"):
            _log("tick")
            time.sleep(0.05)
    threading.Thread(target=noisy, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
