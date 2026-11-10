"""Mock persistent worker — stdio JSON-lines protocol (M4.1 tests).

Emits {"type":"ready"} on start, heartbeats {"type":"hb"}, answers
{"cmd":"generate"} by writing a real PNG and replying
{"type":"result"}, honors {"cmd":"cancel"} and {"cmd":"shutdown"}.

Environment switches for failure injection:
  MOCK_NO_HB=1        — never emit heartbeats (health-check failure)
  MOCK_CRASH_AFTER=N  — os._exit(2) after N commands
  MOCK_CHILD=1        — spawn a sleep child process (tree-kill test)
  MOCK_GEN_DELAY_S=f  — delay each generate step (cancellation window)
  MOCK_NO_READY=1     — never emit ready (startup timeout test)
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time

cmds_seen = 0
cancelled: set[int] = set()
inbox: queue.Queue = queue.Queue()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def heartbeat() -> None:
    if _env("MOCK_NO_HB"):
        return
    interval = float(_env("MOCK_HB_INTERVAL_S", "0.5"))
    while True:
        emit({"type": "hb", "ts": time.monotonic()})
        time.sleep(interval)


def write_png(path: str, seed_text: str) -> str:
    from PIL import Image, ImageDraw
    digest = hashlib.sha256(seed_text.encode()).digest()
    r, g, b = digest[0] // 2 + 40, digest[1] // 2 + 60, digest[2] // 2 + 80
    img = Image.new("RGB", (512, 512), (16, 24, 36))
    draw = ImageDraw.Draw(img)
    for y in range(512):
        t = y / 512
        draw.line([(0, y), (512, y)], fill=(int(r * t), int(g * t), int(b * t)))
    draw.rectangle([24, 24, 488, 488], outline=(114, 221, 205), width=3)
    draw.text((40, 40), "MOCK WORKER OUTPUT — no AI inference", fill=(174, 187, 208))
    img.save(path, format="PNG")
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def crash_check() -> None:
    global cmds_seen
    n = int(_env("MOCK_CRASH_AFTER", "0") or "0")
    if n and cmds_seen >= n:
        os._exit(2)


def stdin_reader() -> None:
    """Reads stdin on its own thread so control messages (cancel,
    shutdown) are visible while a generate command is in flight —
    real workers behave the same way."""
    for line in sys.stdin:
        try:
            inbox.put(json.loads(line))
        except json.JSONDecodeError:
            continue
    inbox.put(None)


def drain_control() -> None:
    """Process pending non-generate commands without blocking."""
    while True:
        try:
            msg = inbox.get_nowait()
        except queue.Empty:
            return
        if msg is None:
            continue
        cmd = msg.get("cmd")
        req_id = msg.get("req_id")
        global cmds_seen
        cmds_seen += 1
        crash_check()
        if cmd == "cancel":
            cancelled.add(req_id)
        elif cmd == "ping":
            emit({"type": "pong", "req_id": req_id})
        elif cmd == "shutdown":
            _delayed_exit(req_id)
        else:
            inbox.put(msg)  # not ours — put back for the main loop
            return


def handle_generate(msg: dict, delay: float) -> None:
    req_id = msg.get("req_id")
    payload = msg.get("payload") or {}
    out_path = payload.get("out_path", "")
    steps = int(payload.get("steps", 4))
    for i in range(steps):
        drain_control()
        if req_id in cancelled:
            emit({"type": "result", "req_id": req_id, "ok": False,
                  "error": "cancelled"})
            return
        time.sleep(delay)
        emit({"type": "progress", "req_id": req_id,
              "pct": (i + 1) / steps})
    try:
        drain_control()
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        sha = write_png(out_path, json.dumps(payload, sort_keys=True))
        if req_id in cancelled:
            emit({"type": "result", "req_id": req_id, "ok": False,
                  "error": "cancelled"})
        else:
            emit({"type": "result", "req_id": req_id, "ok": True,
                  "path": out_path, "sha256": sha})
    except Exception as e:
        emit({"type": "result", "req_id": req_id, "ok": False,
              "error": str(e)})


def main() -> None:
    global cmds_seen
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=stdin_reader, daemon=True).start()

    child_pid = None
    if _env("MOCK_CHILD"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        child_pid = child.pid

    if not _env("MOCK_NO_READY"):
        emit({"type": "ready", "pid": os.getpid(), "child_pid": child_pid,
              "ram_bytes": int(_env("MOCK_RAM_MB", "0") or 0) * 1024 * 1024,
              "vram_bytes": int(_env("MOCK_VRAM_MB", "0") or 0) * 1024 * 1024})

    delay = float(_env("MOCK_GEN_DELAY_S", "0.05"))
    while True:
        msg = inbox.get()
        if msg is None:
            break
        cmd = msg.get("cmd")
        req_id = msg.get("req_id")
        cmds_seen += 1
        crash_check()
        if cmd == "generate":
            handle_generate(msg, delay)
        elif cmd == "cancel":
            cancelled.add(req_id)
        elif cmd == "ping":
            emit({"type": "pong", "req_id": req_id})
        elif cmd == "shutdown":
            _delayed_exit(req_id)


def _delayed_exit(req_id) -> None:
    """Shutdown honoring test envs: MOCK_IGNORE_SHUTDOWN skips the exit
    entirely (forces the supervisor's taskkill fallback), MOCK_EXIT_DELAY_S
    sleeps first (slow graceful teardown — exercises confirm-exit waits)."""
    if _env("MOCK_IGNORE_SHUTDOWN"):
        return
    emit({"type": "bye", "req_id": req_id})
    delay = float(_env("MOCK_EXIT_DELAY_S", "0") or 0)
    if delay:
        time.sleep(delay)
    sys.exit(0)


if __name__ == "__main__":
    main()
