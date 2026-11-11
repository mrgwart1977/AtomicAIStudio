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
At startup the watchdog snapshots every running ``sd-server.exe`` PID as
BASELINE — those belong to a foreign world and are NEVER killed. Only
sd-server processes appearing *after* baseline (the spawned inference
worker) plus their child processes are termination targets, so no
unrelated process can be reaped.

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
the final record carries ``event: "termination"`` with the breach reason
and the killed PID set. Exit codes: 0 = clean (worker finished or never
appeared), 2 = breach terminated the worker tree, 3 = watchdog error.

Usage (from the dev-backend shell, in a SEPARATE terminal):

    backend\\.venv\\Scripts\\python -m app.domain.firstrun_watchdog \\
        --gpu-index 0 --deadline-s 420 \\
        --log %LOCALAPPDATA%\\AtomicAIStudio\\watchdog\\first-run.jsonl

Start it BEFORE committing the generation command and leave it running.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

WORKER_EXE = "sd-server.exe"
MiB = 1024 * 1024


@dataclass
class WatchdogConfig:
    interval_s: float = 1.0
    gpu_index: int = 0
    vram_floor_mib: float = 512.0        # 0.5 GiB free
    ram_floor_mib: float = 1536.0        # 1.5 GiB available
    tree_rss_ceiling_mib: float = 8192.0  # 8 GiB resident across the tree
    deadline_s: float = 420.0            # 7 min wall clock
    confirm_samples: int = 2             # consecutive gauge breaches
    telemetry_loss_samples: int = 3      # consecutive telemetry failures
    expect_workers: int = 1              # >1 post-baseline worker = breach
    worker_name: str = WORKER_EXE
    log_path: Path | None = None
    kill_enabled: bool = True            # --dry-run flips this off


@dataclass
class ProcSnap:
    """Minimal process snapshot the watchdog reasons about."""
    pid: int
    name: str
    create_time: float = 0.0


class Telemetry(Protocol):
    """Telemetry surface — real impl is psutil + nvidia-smi; tests fake it.
    Any method may raise; the watchdog counts consecutive failures and
    fails closed."""

    def ram_available_mib(self) -> float: ...
    def gpu_free_mib(self, index: int) -> float: ...
    def worker_pids(self, name: str) -> list[ProcSnap]: ...
    def tree_pids(self, root_pid: int) -> list[int]: ...
    def rss_bytes(self, pids: Iterable[int]) -> int: ...
    def alive(self, pid: int) -> bool: ...
    def kill_tree(self, pids: Iterable[int]) -> list[int]: ...


# ------------------------------------------------------------------
# real telemetry (Windows / psutil / nvidia-smi)
# ------------------------------------------------------------------

class PsutilTelemetry:
    """Production telemetry: psutil for RAM/process/RSS, nvidia-smi for
    per-GPU free VRAM (same invocation style as domain.hardware)."""

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
                                          create_time=p.info.get("create_time") or 0.0))
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                continue
        return snaps

    def tree_pids(self, root_pid: int) -> list[int]:
        try:
            root = self._psutil.Process(root_pid)
            kids = root.children(recursive=True)
        except self._psutil.NoSuchProcess:
            return []
        return [root_pid, *[k.pid for k in kids]]

    def rss_bytes(self, pids: Iterable[int]) -> int:
        total = 0
        for pid in pids:
            try:
                total += self._psutil.Process(pid).memory_info().rss
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                continue
        return total

    def alive(self, pid: int) -> bool:
        return self._psutil.pid_exists(pid)

    def kill_tree(self, pids: Iterable[int]) -> list[int]:
        """Kill each pid (children are collected by the caller via
        tree_pids before this runs). Returns the pids still alive after
        the grace wait — [] means fully terminated."""
        psutil = self._psutil
        procs = []
        for pid in dict.fromkeys(pids):
            try:
                procs.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                continue
        for p in procs:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        gone, alive = psutil.wait_procs(procs, timeout=10)
        for p in alive:  # last resort: Windows tree-kill
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, timeout=15)
            except subprocess.SubprocessError:
                pass
        _, still = psutil.wait_procs(alive, timeout=5)
        return [p.pid for p in still]


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
    """Observe → evaluate → terminate. Only post-baseline worker PIDs and
    their descendants are ever killed."""

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
        self._baseline_pids: set[int] = set()
        self._tracked_tree: set[int] = set()  # every pid seen in the worker tree
        self._start = 0.0
        self._worker_seen = False
        self._breach_streak = {"vram": 0, "ram": 0, "rss": 0}
        self._telemetry_streak = 0

    # -------------------------------------------------- helpers

    def _record(self, rec: dict[str, Any]) -> None:
        rec.setdefault("ts", round(self.wallclock(), 3))
        self._log.write(rec)

    def _post_baseline_workers(self) -> list[ProcSnap]:
        return [s for s in self.tel.worker_pids(self.cfg.worker_name)
                if s.pid not in self._baseline_pids]

    def _gauge_breach(self) -> str | None:
        """Return a reason once any gauge has breached confirm_samples
        consecutive times; reset streaks on healthy samples."""
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

    # -------------------------------------------------- termination

    def _terminate(self, reason: str) -> int:
        targets = sorted(self._tracked_tree - self._baseline_pids)
        killed: list[int] = []
        survivors: list[int] = []
        if targets and self.cfg.kill_enabled:
            survivors = self.tel.kill_tree(targets)
            killed = [p for p in targets if p not in survivors]
        elif targets:
            killed = []  # dry-run: report what WOULD die
        self._record({
            "event": "termination", "reason": reason,
            "targets": targets, "killed": killed,
            "survivors": survivors,
            "dry_run": not self.cfg.kill_enabled,
        })
        self._log.close()
        return self.EXIT_BREACH

    # -------------------------------------------------- main loop

    def run(self) -> int:
        cfg = self.cfg
        self._start = self.clock()
        try:
            baseline = self.tel.worker_pids(cfg.worker_name)
            self._baseline_pids = {s.pid for s in baseline}
            self._record({"event": "start", "baseline_pids": sorted(self._baseline_pids),
                          "config": {k: (str(v) if isinstance(v, Path) else v)
                                     for k, v in vars(cfg).items()}})
        except Exception as e:  # cannot even enumerate → nothing safe to do
            self._record({"event": "error",
                          "reason": f"baseline process scan failed: {e}"})
            self._log.close()
            return self.EXIT_ERROR

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
                tree_pids: set[int] = set()
                for w in workers:
                    tree_pids.update(self.tel.tree_pids(w.pid))
                self._tracked_tree |= tree_pids
                rss_mib = self.tel.rss_bytes(tree_pids) / MiB
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

            # ---- worker gone → test over (check for orphan stragglers) ----
            if self._worker_seen and not w_pids:
                orphans = sorted(p for p in self._tracked_tree - self._baseline_pids
                                 if self.tel.alive(p))
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
    ap.add_argument("--vram-floor-mib", type=float, default=512.0)
    ap.add_argument("--ram-floor-mib", type=float, default=1536.0)
    ap.add_argument("--tree-rss-ceiling-mib", type=float, default=8192.0)
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
        log_path=args.log, kill_enabled=not args.dry_run)
    try:
        return FirstRunWatchdog(cfg, PsutilTelemetry()).run()
    except KeyboardInterrupt:
        return FirstRunWatchdog.EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
