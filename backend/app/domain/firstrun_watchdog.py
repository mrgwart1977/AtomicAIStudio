"""First-inference resource-safety watchdog (operator-run, standalone).

An INDEPENDENT observer process for the controlled first real-inference
test. It runs in its own OS process — outside the backend — so it keeps
working even if the backend hangs, the job engine wedges, or the normal
``POST /api/jobs/{id}/cancel`` path is unreachable. It never talks to
the backend API and never touches any sd-server HTTP endpoint: it only
reads process/GPU telemetry and kills. It therefore cannot trigger
model loading or inference by itself.

Trust model
-----------
Process identity is ``(pid, create_time)`` — captured at discovery and
re-verified immediately before every kill, including the ``taskkill``
fallback. A pid that was reused shows a different create_time and is
NEVER killed (recorded as ``identity_mismatch``).

At startup the watchdog snapshots every running ``sd-server.exe``
identity as BASELINE — those belong to a foreign world and are never
killed. A baseline pid that is later REUSED by a new sd-server counts
as a fresh post-baseline worker (its identity differs — correctly, it
is not the process that was snapshotted).

Startup gates (must ALL pass before any monitoring — and before the
first-run workflow may proceed):
  1. process enumeration works and the baseline is EMPTY
     (``--allow-baseline`` overrides for advanced use)
  2. GPU (nvidia-smi) + RAM (psutil) telemetry actually respond —
     bounded retries, then refused
  3. readiness minimums: free VRAM >= 4.5 GiB AND available RAM >= 6 GiB
Failure writes ``{"event": "refused"}`` and exits 3. Only after all
gates pass is ``{"event": "ready"}`` written — that record is the
machine-verifiable signal that monitoring is live.

Enforced breaches (defaults match the approved first-run procedure)
------------------------------------------------------------------
- free VRAM on the watched GPU < 0.5 GiB        (2 consecutive samples)
- available system RAM < 1.5 GiB                (2 consecutive samples)
- tracked worker tree RSS > 8 GiB               (2 consecutive samples)
- elapsed test duration > 420 s                 (immediate)
- a second post-baseline sd-server process      (immediate)
- telemetry/monitoring unavailable              (3 consecutive failures — fail closed)

A gauge breach requires ``confirm_samples`` consecutive violations so a
single transient spike cannot kill the run; structural conditions
(deadline, extra worker, monitor loss) have no grace because they are
not noisy signals.

Every ~1 s sample is appended as JSONL (timestamped) to the log file;
the final record carries ``event: "termination"`` with the breach
reason, targets, killed PIDs, survivors and any identity mismatches.
Exit codes: 0 = clean (worker finished or never appeared),
2 = breach terminated the worker tree, 3 = watchdog error/refused.

Usage (from the dev-backend shell, in a SEPARATE terminal):

    backend\\.venv\\Scripts\\python -m app.domain.firstrun_watchdog \\
        --gpu-index 0 --deadline-s 420 \\
        --log %LOCALAPPDATA%\\AtomicAIStudio\\watchdog\\first-run.jsonl

Start it BEFORE committing the generation command; proceed only once
the ``"event": "ready"`` record appears. Leave it running.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

WORKER_EXE = "sd-server.exe"
MiB = 1024 * 1024


@dataclass
class WatchdogConfig:
    interval_s: float = 1.0
    gpu_index: int = 0
    vram_floor_mib: float = 512.0         # in-run breach floor (0.5 GiB)
    ram_floor_mib: float = 1536.0         # in-run breach floor (1.5 GiB)
    tree_rss_ceiling_mib: float = 8192.0  # 8 GiB resident across the tree
    deadline_s: float = 420.0             # 7 min wall clock
    confirm_samples: int = 2              # consecutive gauge breaches
    telemetry_loss_samples: int = 3       # consecutive telemetry failures
    expect_workers: int = 1               # >1 post-baseline worker = breach
    worker_name: str = WORKER_EXE
    # Startup readiness gates (the stricter external requirements)
    min_free_vram_mib: float = 4608.0     # 4.5 GiB free VRAM to declare ready
    min_free_ram_mib: float = 6144.0      # 6 GiB available RAM to declare ready
    startup_probe_attempts: int = 5       # bounded retries before refused
    allow_baseline: bool = False          # refuse non-empty baseline by default
    log_path: Path | None = None
    kill_enabled: bool = True             # --dry-run flips this off


@dataclass(frozen=True)
class ProcSnap:
    """Process identity — (pid, create_time). Identity must match at kill
    time or the process is treated as a DIFFERENT process (pid reuse)."""
    pid: int
    name: str
    create_time: float = 0.0


@dataclass
class KillReport:
    killed: list[int]
    survived: list[int]
    identity_mismatch: list[int]


class Telemetry(Protocol):
    """Telemetry surface — real impl is psutil + nvidia-smi; tests fake it.
    Any method may raise; the watchdog counts consecutive failures and
    fails closed."""

    def ram_available_mib(self) -> float: ...
    def gpu_free_mib(self, index: int) -> float: ...
    def worker_pids(self, name: str) -> list[ProcSnap]: ...
    def tree_snaps(self, root: ProcSnap) -> list[ProcSnap]: ...
    def rss_bytes(self, pids: Iterable[int]) -> int: ...
    def identity_alive(self, pid: int, create_time: float) -> bool: ...
    def kill_tree(self, targets: list[ProcSnap]) -> KillReport: ...


# ------------------------------------------------------------------
# real telemetry (Windows / psutil / nvidia-smi)
# ------------------------------------------------------------------

class PsutilTelemetry:
    """Production telemetry: psutil for RAM/process/RSS/identity,
    nvidia-smi for per-GPU free VRAM (same invocation style as
    domain.hardware)."""

    def __init__(self, smi_timeout_s: float = 8.0) -> None:
        import psutil  # deferred — tests never touch this class
        self._psutil = psutil
        self._smi = shutil.which("nvidia-smi")
        self._smi_timeout = smi_timeout_s

    def ram_available_mib(self) -> float:
        return self._psutil.virtual_memory().available / MiB

    def gpu_free_mib(self, index: int) -> float:
        if not self._smi:
            raise RuntimeError("nvidia-smi not on PATH — GPU telemetry unavailable")
        out = subprocess.run(
            [self._smi, "--query-gpu=index,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=self._smi_timeout)
        if out.returncode != 0:
            raise RuntimeError(f"nvidia-smi exited {out.returncode}")
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0] == str(index):
                return float(parts[1])
        raise RuntimeError(f"GPU index {index} not found in nvidia-smi output")

    def worker_pids(self, name: str) -> list[ProcSnap]:
        lname = name.lower()
        snaps = []
        for p in self._psutil.process_iter(["name", "create_time"]):
            try:
                if (p.info.get("name") or "").lower() == lname:
                    snaps.append(ProcSnap(pid=p.pid, name=p.info["name"],
                                          create_time=float(p.info.get("create_time") or 0.0)))
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                continue
        return snaps

    def tree_snaps(self, root: ProcSnap) -> list[ProcSnap]:
        """Root + recursive descendants, each WITH its create_time so the
        kill path can verify identity before terminating."""
        try:
            rp = self._psutil.Process(root.pid)
            if rp.create_time() != root.create_time:
                return []  # pid reused — not our process anymore
            kids = rp.children(recursive=True)
        except self._psutil.NoSuchProcess:
            return []
        snaps = [root]
        for k in kids:
            try:
                snaps.append(ProcSnap(pid=k.pid, name=k.name(),
                                      create_time=k.create_time()))
            except self._psutil.NoSuchProcess:
                continue
        return snaps

    def rss_bytes(self, pids: Iterable[int]) -> int:
        total = 0
        for pid in pids:
            try:
                total += self._psutil.Process(pid).memory_info().rss
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                continue
        return total

    def identity_alive(self, pid: int, create_time: float) -> bool:
        """Alive AND still the same process (pid-reuse guard)."""
        try:
            p = self._psutil.Process(pid)
        except self._psutil.NoSuchProcess:
            return False
        try:
            return p.create_time() == create_time
        except self._psutil.NoSuchProcess:
            return False

    def kill_tree(self, targets: list[ProcSnap]) -> KillReport:
        """Kill each identity-verified target. A pid whose create_time no
        longer matches was reused — it is a DIFFERENT process and is
        never signaled. The taskkill fallback re-verifies identity right
        before firing and uses /PID only (never /T — /T would reap the
        unrelated children of a reused pid)."""
        psutil = self._psutil
        killed: list[int] = []
        survived: list[int] = []
        mismatch: list[int] = []
        procs = []
        for t in targets:
            try:
                p = psutil.Process(t.pid)
                if p.create_time() != t.create_time:
                    mismatch.append(t.pid)
                    continue
                procs.append(p)
            except psutil.NoSuchProcess:
                continue  # already gone — nothing to kill
        for p in procs:
            try:
                p.kill()
                killed.append(p.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _, alive = psutil.wait_procs(procs, timeout=10)
        for p, t in ((p, t) for p in alive for t in targets if t.pid == p.pid):
            try:  # identity re-check immediately before the fallback
                if p.create_time() != t.create_time:
                    mismatch.append(p.pid)
                    continue
            except psutil.NoSuchProcess:
                continue
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(p.pid)],
                               capture_output=True, timeout=15)
                killed.append(p.pid)
            except subprocess.SubprocessError:
                survived.append(p.pid)
        _, still = psutil.wait_procs(alive, timeout=5)
        survived.extend(x.pid for x in still if x.pid not in survived)
        return KillReport(killed=killed, survived=survived,
                          identity_mismatch=mismatch)


# ------------------------------------------------------------------
# watchdog core
# ------------------------------------------------------------------

class _SampleLogger:
    def __init__(self, path: Path | None) -> None:
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8") if path else None

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True)
        with self._lock:
            if self._fh:
                self._fh.write(line + "\n")
                self._fh.flush()
            print(line, flush=True)

    def close(self) -> None:
        if self._fh:
            self._fh.close()


class FirstRunWatchdog:
    """Gate → observe → evaluate → terminate. Only post-baseline worker
    identities are ever killed, and only when their create_time still
    matches the recorded one."""

    EXIT_CLEAN, EXIT_BREACH, EXIT_ERROR = 0, 2, 3

    def __init__(self, cfg: WatchdogConfig, telemetry: Telemetry,
                 clock: Callable[[], float] = time.monotonic,
                 wallclock: Callable[[], float] = time.time,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.tel = telemetry
        self.clock = clock
        self.wallclock = wallclock
        self.sleeper = sleeper
        self._log = _SampleLogger(cfg.log_path)
        self._baseline: dict[int, float] = {}   # pid -> create_time
        self._tracked_tree: dict[int, float] = {}  # pid -> create_time
        self._start = 0.0
        self._worker_seen = False
        self._breach_streak = {"vram": 0, "ram": 0, "rss": 0}
        self._telemetry_streak = 0

    # -------------------------------------------------- helpers

    def _record(self, rec: dict[str, Any]) -> None:
        rec.setdefault("ts", round(self.wallclock(), 3))
        self._log.write(rec)

    def _refuse(self, reason: str) -> int:
        self._record({"event": "refused", "reason": reason})
        self._log.close()
        return self.EXIT_ERROR

    def _is_post_baseline(self, snap: ProcSnap) -> bool:
        """Not in baseline — OR a baseline pid whose create_time changed
        (reused): identity differs, so it is a NEW process to watch."""
        return self._baseline.get(snap.pid) != snap.create_time

    def _post_baseline_workers(self) -> list[ProcSnap]:
        return [s for s in self.tel.worker_pids(self.cfg.worker_name)
                if self._is_post_baseline(s)]

    def _gauge_breach(self) -> str | None:
        reasons = []
        if self._breach_streak["vram"] >= self.cfg.confirm_samples:
            reasons.append(f"gpu free VRAM below {self.cfg.vram_floor_mib:.0f} MiB "
                           f"for {self._breach_streak['vram']} consecutive samples")
        if self._breach_streak["ram"] >= self.cfg.confirm_samples:
            reasons.append(f"available RAM below {self.cfg.ram_floor_mib:.0f} MiB "
                           f"for {self._breach_streak['ram']} consecutive samples")
        if self._breach_streak["rss"] >= self.cfg.confirm_samples:
            reasons.append(f"worker tree RSS above {self.cfg.tree_rss_ceiling_mib:.0f} MiB "
                           f"for {self._breach_streak['rss']} consecutive samples")
        return "; ".join(reasons) or None

    # -------------------------------------------------- startup gates

    def _startup_gates(self) -> int | None:
        """None = all gates passed; an exit code = refused."""
        cfg = self.cfg
        # gate 1: process enumeration + empty baseline
        try:
            baseline = self.tel.worker_pids(cfg.worker_name)
        except Exception as e:
            return self._refuse(f"baseline process scan failed: {e}")
        self._baseline = {s.pid: s.create_time for s in baseline}
        self._record({"event": "start",
                      "baseline_pids": sorted(self._baseline)})
        if self._baseline and not cfg.allow_baseline:
            return self._refuse(
                f"non-empty baseline — {len(self._baseline)} sd-server.exe "
                f"process(es) already running {sorted(self._baseline)}; "
                "terminate them or pass --allow-baseline")
        # gate 2+3: telemetry responds AND meets readiness minimums
        last_err: Exception | None = None
        ram_mib = gpu_mib = 0.0
        for attempt in range(1, max(1, cfg.startup_probe_attempts) + 1):
            try:
                ram_mib = self.tel.ram_available_mib()
                gpu_mib = self.tel.gpu_free_mib(cfg.gpu_index)
                last_err = None
                break
            except Exception as e:
                last_err = e
                self._record({"event": "startup_probe",
                              "attempt": attempt, "error": str(e)})
                self.sleeper(cfg.interval_s)
        if last_err is not None:
            return self._refuse(
                f"telemetry unavailable at startup ({last_err}) — "
                "refusing to arm (fail closed)")
        if gpu_mib < cfg.min_free_vram_mib:
            return self._refuse(
                f"insufficient free VRAM at startup: {gpu_mib:.0f} MiB "
                f"< {cfg.min_free_vram_mib:.0f} MiB required")
        if ram_mib < cfg.min_free_ram_mib:
            return self._refuse(
                f"insufficient available RAM at startup: {ram_mib:.0f} MiB "
                f"< {cfg.min_free_ram_mib:.0f} MiB required")
        # all gates passed — machine-verifiable readiness
        self._record({"event": "ready", "telemetry_ok": True,
                      "gpu_free_mib": round(gpu_mib, 1),
                      "ram_available_mib": round(ram_mib, 1),
                      "baseline_pids": sorted(self._baseline),
                      "config": {k: (str(v) if isinstance(v, Path) else v)
                                 for k, v in vars(cfg).items()}})
        return None

    # -------------------------------------------------- termination

    def _terminate(self, reason: str) -> int:
        # Best-effort final sweep: a fail-closed breach can fire before
        # any healthy sample recorded the worker (e.g. GPU telemetry dead
        # from launch). Re-scan so the kill list is as complete as
        # psutil still allows — identity-filtered below.
        try:
            for w in self._post_baseline_workers():
                for s in self.tel.tree_snaps(w):
                    self._tracked_tree[s.pid] = s.create_time
        except Exception:
            pass
        # Baseline exclusion is identity-based, not pid-based: a baseline
        # pid REUSED by a fresh worker is a different process and stays
        # a target — only the originally snapshotted identity is immune.
        targets = [ProcSnap(pid=p, name="", create_time=ct)
                   for p, ct in sorted(self._tracked_tree.items())
                   if self._baseline.get(p) != ct]
        report = KillReport(killed=[], survived=[], identity_mismatch=[])
        if targets and self.cfg.kill_enabled:
            report = self.tel.kill_tree(targets)
        self._record({
            "event": "termination", "reason": reason,
            "targets": [t.pid for t in targets],
            "killed": report.killed, "survivors": report.survived,
            "identity_mismatch": report.identity_mismatch,
            "dry_run": not self.cfg.kill_enabled,
        })
        self._log.close()
        return self.EXIT_BREACH

    # -------------------------------------------------- main loop

    def run(self) -> int:
        cfg = self.cfg
        self._start = self.clock()
        refused = self._startup_gates()
        if refused is not None:
            return refused

        while True:
            elapsed = self.clock() - self._start
            if elapsed > cfg.deadline_s:
                return self._terminate(
                    f"test duration exceeded {cfg.deadline_s:.0f}s deadline")

            # ---- sample (any failure counts toward fail-closed) ----
            try:
                ram_mib = self.tel.ram_available_mib()
                gpu_mib = self.tel.gpu_free_mib(cfg.gpu_index)
                workers = self._post_baseline_workers()
                tree_snaps: list[ProcSnap] = []
                for w in workers:
                    tree_snaps.extend(self.tel.tree_snaps(w))
                for s in tree_snaps:
                    self._tracked_tree[s.pid] = s.create_time
                rss_mib = self.tel.rss_bytes(
                    [s.pid for s in tree_snaps]) / MiB
                self._telemetry_streak = 0
            except Exception as e:
                self._telemetry_streak += 1
                self._record({"event": "sample_error", "elapsed_s": round(elapsed, 2),
                              "error": str(e), "streak": self._telemetry_streak})
                if self._telemetry_streak >= cfg.telemetry_loss_samples:
                    return self._terminate(
                        f"monitoring unavailable ({e}) — failing closed "
                        f"after {self._telemetry_streak} consecutive failures")
                self.sleeper(cfg.interval_s)
                continue

            w_pids = sorted(w.pid for w in workers)
            self._worker_seen = self._worker_seen or bool(w_pids)
            self._record({
                "event": "sample", "elapsed_s": round(elapsed, 2),
                "ram_available_mib": round(ram_mib, 1),
                "gpu_free_mib": round(gpu_mib, 1),
                "worker_pids": w_pids,
                "tree_rss_mib": round(rss_mib, 1),
            })

            # ---- structural breaches (immediate) ----
            if len(w_pids) > cfg.expect_workers:
                return self._terminate(
                    f"{len(w_pids)} sd-server processes detected "
                    f"(expected {cfg.expect_workers}): {w_pids}")

            # ---- gauge breaches (consecutive) ----
            self._breach_streak["vram"] = (
                self._breach_streak["vram"] + 1 if gpu_mib < cfg.vram_floor_mib else 0)
            self._breach_streak["ram"] = (
                self._breach_streak["ram"] + 1 if ram_mib < cfg.ram_floor_mib else 0)
            self._breach_streak["rss"] = (
                self._breach_streak["rss"] + 1
                if w_pids and rss_mib > cfg.tree_rss_ceiling_mib else 0)
            reason = self._gauge_breach()
            if reason:
                return self._terminate(reason)

            # ---- worker gone → test over (identity-checked orphans) ----
            if self._worker_seen and not w_pids:
                orphans = sorted(p for p, ct in self._tracked_tree.items()
                                 if self._baseline.get(p) != ct
                                 and self.tel.identity_alive(p, ct))
                if orphans:
                    return self._terminate(
                        f"worker exited but child processes survived: {orphans}")
                self._record({"event": "complete",
                              "reason": "worker exited cleanly", "elapsed_s": round(elapsed, 2)})
                self._log.close()
                return self.EXIT_CLEAN

            self.sleeper(cfg.interval_s)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="first-run watchdog",
        description="Independent resource-safety watchdog for the first "
                    "real-inference test. Run in a separate terminal; it "
                    "never talks to the backend or the sd-server API.")
    ap.add_argument("--gpu-index", type=int, default=0,
                    help="nvidia-smi GPU index to watch (default 0)")
    ap.add_argument("--interval-s", type=float, default=1.0)
    ap.add_argument("--deadline-s", type=float, default=420.0,
                    help="hard wall-clock deadline from watchdog start (default 420)")
    ap.add_argument("--vram-floor-mib", type=float, default=512.0,
                    help="in-run breach floor (default 512 = 0.5 GiB)")
    ap.add_argument("--ram-floor-mib", type=float, default=1536.0,
                    help="in-run breach floor (default 1536 = 1.5 GiB)")
    ap.add_argument("--tree-rss-ceiling-mib", type=float, default=8192.0)
    ap.add_argument("--min-free-vram-mib", type=float, default=4608.0,
                    help="readiness gate: free VRAM required to declare "
                         "ready (default 4608 = 4.5 GiB)")
    ap.add_argument("--min-free-ram-mib", type=float, default=6144.0,
                    help="readiness gate: available RAM required to "
                         "declare ready (default 6144 = 6 GiB)")
    ap.add_argument("--startup-probe-attempts", type=int, default=5)
    ap.add_argument("--allow-baseline", action="store_true",
                    help="permit pre-existing sd-server.exe processes "
                         "(default: refuse)")
    ap.add_argument("--log", type=Path, default=None,
                    help="JSONL sample log path (also mirrored to stdout)")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and log breaches without killing")
    args = ap.parse_args(argv)

    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
    cfg = WatchdogConfig(
        gpu_index=args.gpu_index, interval_s=args.interval_s,
        deadline_s=args.deadline_s, vram_floor_mib=args.vram_floor_mib,
        ram_floor_mib=args.ram_floor_mib,
        tree_rss_ceiling_mib=args.tree_rss_ceiling_mib,
        min_free_vram_mib=args.min_free_vram_mib,
        min_free_ram_mib=args.min_free_ram_mib,
        startup_probe_attempts=args.startup_probe_attempts,
        allow_baseline=args.allow_baseline,
        log_path=args.log, kill_enabled=not args.dry_run)
    try:
        return FirstRunWatchdog(cfg, PsutilTelemetry()).run()
    except KeyboardInterrupt:
        return FirstRunWatchdog.EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
