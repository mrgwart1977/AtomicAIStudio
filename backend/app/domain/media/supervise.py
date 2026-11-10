"""Supervised subprocess execution for media tools.

Every ffmpeg/ffprobe invocation goes through proc_guard's Windows Job
Object supervision: suspended spawn → kill-on-close assignment → resume,
fail-closed. Adds bounded runtime, cancellation, and captured output —
media files are untrusted input, so these are the same contracts the
inference workers run under.
"""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Any

from app.domain import proc_guard

_DEFAULT_TIMEOUT_S = 120.0
_POLL_S = 0.25
_TAIL_BYTES = 4096


class MediaToolError(RuntimeError):
    """Tool could not be spawned/supervised — fail closed."""


def run(argv: list[str], *, timeout_s: float = _DEFAULT_TIMEOUT_S,
        cancel: threading.Event | None = None) -> dict[str, Any]:
    """Run argv under job-object supervision. Never uses a shell.

    Returns {exit, output, timed_out, cancelled, elapsed_ms}. `exit` is
    None when the process never completed (timeout/cancel killed it)."""
    started = time.monotonic()
    try:
        proc = proc_guard.spawn_supervised(argv, stdout=subprocess.PIPE)
    except Exception as exc:
        raise MediaToolError(f"supervised spawn failed: {exc}") from exc

    timed_out = cancelled = False
    output = b""
    deadline = started + timeout_s
    try:
        while True:
            try:
                out, _ = proc.communicate(timeout=_POLL_S)
                output = out or b""
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    proc_guard.terminate_tree(proc.pid)
                elif time.monotonic() >= deadline:
                    timed_out = True
                    proc_guard.terminate_tree(proc.pid)
                else:
                    continue
                # reaped after kill: collect whatever output exists
                try:
                    out, _ = proc.communicate(timeout=5)
                    output = out or b""
                except subprocess.TimeoutExpired:
                    pass
                break
    finally:
        proc_guard.unsupervise(proc.pid)

    text = output.decode("utf-8", "replace")
    return {
        "exit": proc.returncode if not (timed_out or cancelled) else None,
        "output_tail": text[-_TAIL_BYTES:],
        "timed_out": timed_out,
        "cancelled": cancelled,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
