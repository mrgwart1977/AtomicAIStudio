"""Worker wire protocols.

Two implementations:

- ``StdioJsonLinesProtocol`` — one JSON object per line on stdin/stdout.
  Non-JSON stdout lines are captured as worker logs (stderr is merged
  into stdout by proc_guard, so log noise can never corrupt framing).
  Used by mock workers and any future stdio-protocol runtime.

- ``SdServerHttpProtocol`` — the sd.cpp ``sd-server`` HTTP surface
  verified on the pinned master-1073 runtime: ``GET /v1/models`` for
  readiness/health, ``POST /v1/images/generations`` for requests.
  Not wired to real inference in Phase 1 — exercised in tests against
  an in-process HTTP stub and (readiness only) the real binary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

ProgressFn = Callable[[dict[str, Any]], None]


class WorkerProtocol:
    """Abstract request/health/shutdown channel to one worker process."""

    async def wait_ready(self, timeout_s: float) -> bool:
        raise NotImplementedError

    async def request(
        self,
        cmd: str,
        payload: dict[str, Any],
        *,
        on_progress: ProgressFn | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        timeout_s: float = 600.0,
    ) -> dict[str, Any]:
        """Send a command and return the terminal result dict."""
        raise NotImplementedError

    def last_heartbeat(self) -> float | None:
        """Monotonic time of the last liveness signal, if the protocol
        produces them. None = protocol provides no heartbeat."""
        return None

    async def health_probe(self) -> bool:
        """Active liveness check for protocols without push heartbeats
        (e.g. HTTP). The supervisor calls this on heartbeat_interval
        cadence; implementations should update last_heartbeat() on
        success. Default: passive — protocol has no probe."""
        return True

    async def shutdown(self, grace_s: float = 5.0) -> None:
        raise NotImplementedError

    def log_tail(self, n: int = 20) -> list[str]:
        return []


class StdioJsonLinesProtocol(WorkerProtocol):
    """JSON-lines-over-pipes protocol for stdio workers.

    Outbound commands: ``{"cmd": ..., "req_id": N, "payload": {...}}``
    Inbound events: ``{"type": "ready"|"hb"|"progress"|"result"|"log", ...}``
    A request resolves on ``{"type":"result","req_id":N}``. Cancellation
    sends ``{"cmd":"cancel","req_id":N}`` once — the worker's response
    decides; the supervisor may still kill on its own timeout.
    """

    def __init__(self, proc) -> None:
        self._proc = proc
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._logs: deque[str] = deque(maxlen=200)
        self._last_hb: float | None = None
        self._next_req = 0
        self._write_lock = threading.Lock()
        self._dead = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"worker-reader-{proc.pid}")
        self._reader.start()

    # ---------- reader ----------

    def _read_loop(self) -> None:
        out = self._proc.stdout
        try:
            while True:
                line = out.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if not text:
                    continue
                try:
                    ev = json.loads(text)
                except json.JSONDecodeError:
                    self._logs.append(text)
                    continue
                if not isinstance(ev, dict) or "type" not in ev:
                    self._logs.append(text)
                    continue
                if ev["type"] == "hb":
                    self._last_hb = ev.get("ts") or __import__("time").monotonic()
                elif ev["type"] == "log":
                    self._logs.append(str(ev.get("msg", "")))
                    continue
                self._events.put_nowait(ev)
        except (ValueError, OSError):
            pass
        self._dead.set()

    # ---------- protocol ----------

    async def wait_ready(self, timeout_s: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                ev = await asyncio.wait_for(self._events.get(), timeout=0.25)
            except asyncio.TimeoutError:
                if self._dead.is_set() or self._proc.poll() is not None:
                    return False
                continue
            if ev.get("type") == "ready":
                return True
            if ev.get("type") == "result" and ev.get("ok") is False:
                return False
        return False

    async def request(
        self,
        cmd: str,
        payload: dict[str, Any],
        *,
        on_progress: ProgressFn | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        timeout_s: float = 600.0,
    ) -> dict[str, Any]:
        if self._proc.stdin is None or self._dead.is_set():
            raise RuntimeError("worker stdio channel is closed")
        self._next_req += 1
        req_id = self._next_req
        msg = {"cmd": cmd, "req_id": req_id, "payload": payload}
        self._send(msg)
        deadline = asyncio.get_running_loop().time() + timeout_s
        cancel_sent = False
        while asyncio.get_running_loop().time() < deadline:
            if is_cancelled is not None and is_cancelled() and not cancel_sent:
                self._send({"cmd": "cancel", "req_id": req_id, "payload": {}})
                cancel_sent = True
            try:
                ev = await asyncio.wait_for(self._events.get(), timeout=0.2)
            except asyncio.TimeoutError:
                if self._dead.is_set():
                    raise RuntimeError("worker died mid-request")
                continue
            t = ev.get("type")
            if t == "result" and ev.get("req_id") == req_id:
                return ev
            if t == "progress" and ev.get("req_id") == req_id and on_progress:
                on_progress(ev)
        raise TimeoutError(f"worker request {cmd} timed out after {timeout_s}s")

    def _send(self, msg: dict[str, Any]) -> None:
        data = (json.dumps(msg) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise RuntimeError(f"worker stdin write failed: {e}") from e

    def last_heartbeat(self) -> float | None:
        return self._last_hb

    async def shutdown(self, grace_s: float = 5.0) -> None:
        try:
            self._send({"cmd": "shutdown", "req_id": 0, "payload": {}})
        except RuntimeError:
            return
        deadline = asyncio.get_running_loop().time() + grace_s
        while asyncio.get_running_loop().time() < deadline:
            if self._proc.poll() is not None:
                return
            await asyncio.sleep(0.1)

    def log_tail(self, n: int = 20) -> list[str]:
        return list(self._logs)[-n:]


class SdServerHttpProtocol(WorkerProtocol):
    """sd.cpp sd-server HTTP protocol (verified on pinned master-1073):

    - ``GET /v1/models`` → 200 = server up and accepting requests
    - ``POST /v1/images/generations`` → image generation
    - ``POST /sdapi/v1/txt2img`` → alternate A1111-style endpoint

    Health is an HTTP GET on a short timeout — the server has no push
    channel. The spawned process's stdout IS drained (into a bounded log
    deque): sd-server logs on every request and would block on a full
    pipe buffer if nobody read it.
    Model swap/unload endpoints are NOT verified and intentionally unused.

    Request semantics (Phase 2A contract):
    - a *coherent* HTTP response (any status with a readable body) means
      the server is alive and sane → return the result, worker reusable
    - transport failure / read timeout / abandoned-cancel → the request
      outcome is UNKNOWN — raise so the supervisor kills the worker;
      never return it to the idle pool.
    """

    def __init__(self, base_url: str, http_timeout_s: float = 5.0,
                 proc=None) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = http_timeout_s
        self._last_hb: float | None = None
        self._logs: deque[str] = deque(maxlen=200)
        self._proc = proc
        if proc is not None and getattr(proc, "stdout", None) is not None:
            threading.Thread(target=self._drain_stdout, daemon=True,
                             name=f"sdserver-stdout-{proc.pid}").start()

    def _drain_stdout(self) -> None:
        try:
            for raw in iter(self._proc.stdout.readline, b""):
                self._logs.append(raw.decode("utf-8", "replace").rstrip())
        except (ValueError, OSError):
            pass

    def last_heartbeat(self) -> float | None:
        return self._last_hb

    def log_tail(self, n: int = 20) -> list[str]:
        return list(self._logs)[-n:]

    async def health_probe(self) -> bool:
        """GET /v1/models — 200 keeps the worker alive; anything else is
        NOT immediate death (a busy server may be slow) — the heartbeat
        timestamp simply doesn't advance, so sustained failure goes stale
        and the supervisor kills it."""
        loop = asyncio.get_running_loop()
        code, _ = await loop.run_in_executor(
            None, self._get, "/v1/models", 2.0)
        if code == 200:
            self._last_hb = time.monotonic()
            return True
        return False

    def _get(self, path: str, timeout: float | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(self._base + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self._timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError):
            return 0, b""

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> tuple[int, dict]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(self._base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                return e.code, {"error": f"http {e.code}"}
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return 0, {"error": str(e)}

    async def wait_ready(self, timeout_s: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            # A dead process can never become ready — fail fast rather
            # than burning the whole timeout (e.g. bind failure exits).
            if self._proc is not None and self._proc.poll() is not None:
                return False
            code, _ = await loop.run_in_executor(None, self._get, "/v1/models", 2.0)
            if code == 200:
                self._last_hb = time.monotonic()
                return True
            await asyncio.sleep(0.3)
        return False

    async def request(
        self,
        cmd: str,
        payload: dict[str, Any],
        *,
        on_progress: ProgressFn | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        timeout_s: float = 600.0,
    ) -> dict[str, Any]:
        # sd-server has no mid-request cancel endpoint in this build —
        # cancellation abandons the request and the supervisor kills the
        # worker; a clean HTTP response is the only trustworthy outcome.
        if cmd != "generate":
            raise ValueError(f"unsupported sd-server command: {cmd}")
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            None, self._post, "/v1/images/generations", payload, timeout_s)
        # Poll for cancellation/abandonment while the blocking HTTP call
        # runs — a disconnect does NOT stop server-side inference, so an
        # abandoned request means the worker is recycled (supervisor).
        while True:
            if is_cancelled is not None and is_cancelled():
                raise TimeoutError(
                    "request abandoned by cancellation — worker recycled")
            done, _ = await asyncio.wait({future}, timeout=0.25)
            if done:
                break
        code, body = future.result()
        if code == 0:
            # Transport failure/read timeout — outcome unknown, the
            # server may still be generating. Raise → worker recycled.
            err = str(body.get("error") or "")
            if "timed out" in err.lower() or "timeout" in err.lower():
                raise TimeoutError(f"sd-server request timed out: {err}")
            raise RuntimeError(
                f"sd-server request transport failure: {err}")
        if code == 200:
            return {"type": "result", "ok": True, "http": code, "body": body}
        return {"type": "result", "ok": False, "http": code,
                "error": body.get("error") or f"http {code}"}

    async def shutdown(self, grace_s: float = 5.0) -> None:
        # No HTTP shutdown endpoint — the supervisor terminates the
        # process; this is a no-op by design.
        return None
