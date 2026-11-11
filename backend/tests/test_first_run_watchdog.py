"""Deterministic tests for the first-run inference watchdog.

Telemetry and the clock are faked — no real GPU, RAM pressure, model
weights, or inference. The only real-OS test spawns synthetic Python
process trees to verify tree enumeration and identity-checked
termination on Windows.

Every test that needs a running worker adds it AFTER run() starts (via
the sleeper hook) — anything present at run() entry is the baseline
snapshot and is deliberately never killed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.domain.firstrun_watchdog import (
    FirstRunWatchdog, KillReport, ProcSnap, PsutilTelemetry, WatchdogConfig)

WORKER = "sd-server.exe"
MIB = 1024 * 1024


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeTelemetry:
    """Scripted telemetry. `procs` maps pid -> {name, children, rss, ctime};
    every key present is alive. Identity is (pid, ctime) — a changed
    ctime models OS pid reuse."""

    def __init__(self) -> None:
        self.procs: dict[int, dict] = {}
        self.ram_seq: list[float] = [8192.0]
        self.gpu_seq: list[float] = [7000.0]
        self.fail_gpu: list[Exception] = []
        self.fail_ram: list[Exception] = []
        self.killed: list[int] = []
        self.mismatched: list[int] = []
        self.kill_calls = 0

    def _next(self, seq: list[float]) -> float:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def ram_available_mib(self) -> float:
        if self.fail_ram:
            raise self.fail_ram.pop(0)
        return self._next(self.ram_seq)

    def gpu_free_mib(self, index: int) -> float:
        if self.fail_gpu:
            raise self.fail_gpu.pop(0)
        return self._next(self.gpu_seq)

    def worker_pids(self, name: str) -> list[ProcSnap]:
        return [ProcSnap(pid=p, name=d["name"], create_time=d["ctime"])
                for p, d in self.procs.items()
                if d["name"].lower() == name.lower()]

    def tree_snaps(self, root: ProcSnap) -> list[ProcSnap]:
        if root.pid not in self.procs or \
                self.procs[root.pid]["ctime"] != root.create_time:
            return []
        out = [root]
        for c in self.procs[root.pid]["children"]:
            d = self.procs.get(c)
            if d:
                out.append(ProcSnap(pid=c, name=d["name"], create_time=d["ctime"]))
        return out

    def rss_bytes(self, pids) -> int:
        return sum(self.procs[p]["rss"] for p in pids if p in self.procs)

    def identity_alive(self, pid: int, create_time: float) -> bool:
        d = self.procs.get(pid)
        return bool(d) and d["ctime"] == create_time

    def kill_tree(self, targets: list[ProcSnap]) -> KillReport:
        self.kill_calls += 1
        killed, survived, mismatch = [], [], []
        for t in targets:
            d = self.procs.get(t.pid)
            if d is None:
                continue                       # already dead
            if d["ctime"] != t.create_time:
                mismatch.append(t.pid)         # pid reused — never kill
                self.mismatched.append(t.pid)
                continue
            del self.procs[t.pid]
            killed.append(t.pid)
            self.killed.append(t.pid)
        return KillReport(killed=killed, survived=survived,
                          identity_mismatch=mismatch)

    def add(self, pid: int, name: str = WORKER, children=(),
            rss: int = 512 * MIB, ctime: float = 1.0):
        self.procs[pid] = {"name": name, "children": list(children),
                           "rss": rss, "ctime": ctime}


def _wd(tel: FakeTelemetry, log_path: Path | None = None, **over) -> FirstRunWatchdog:
    cfg = WatchdogConfig(log_path=log_path, **over)
    clk = FakeClock()
    return FirstRunWatchdog(cfg, tel, clock=clk, wallclock=clk, sleeper=clk.sleep)


def _on_sleep(wd: FirstRunWatchdog, fn):
    """Run `fn(call_count)` at the end of every loop iteration."""
    orig = wd.sleeper
    calls = {"n": 0}

    def sleeper(s):
        calls["n"] += 1
        fn(calls["n"])
        orig(s)
    wd.sleeper = sleeper
    return calls


def _events(log_path: Path) -> list[dict]:
    return [json.loads(l) for l in log_path.read_text().splitlines()]


# ------------------------------------------------------------ startup gates

class TestStartupGates:
    def test_clean_baseline_and_healthy_telemetry_writes_ready(self, tmp_path):
        tel = FakeTelemetry()
        wd = _wd(tel, log_path=tmp_path / "w.jsonl")
        _on_sleep(wd, lambda n: tel.add(100) if n == 1 else
                  tel.procs.clear() if n == 3 else None)
        assert wd.run() == FirstRunWatchdog.EXIT_CLEAN
        evs = _events(tmp_path / "w.jsonl")
        kinds = [e["event"] for e in evs]
        assert kinds[0] == "start" and "ready" in kinds and kinds[-1] == "complete"
        ready = next(e for e in evs if e["event"] == "ready")
        assert ready["telemetry_ok"] is True and ready["baseline_pids"] == []
        assert ready["gpu_free_mib"] > 0 and ready["ram_available_mib"] > 0

    def test_refuses_nonempty_baseline_by_default(self, tmp_path):
        tel = FakeTelemetry()
        tel.add(50)  # pre-existing sd-server.exe
        wd = _wd(tel, log_path=tmp_path / "w.jsonl")
        assert wd.run() == FirstRunWatchdog.EXIT_ERROR
        evs = _events(tmp_path / "w.jsonl")
        assert evs[-1]["event"] == "refused" and "non-empty baseline" in evs[-1]["reason"]
        assert not any(e["event"] == "ready" for e in evs)
        assert tel.kill_calls == 0

    def test_allow_baseline_permits_preexisting(self):
        tel = FakeTelemetry()
        tel.add(50)
        wd = _wd(tel, allow_baseline=True, deadline_s=8.0)
        _on_sleep(wd, lambda n: tel.add(100) if n == 1 else None)
        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [100] and tel.identity_alive(50, 1.0)

    def test_refuses_when_gpu_telemetry_dead_at_launch(self, tmp_path):
        tel = FakeTelemetry()
        tel.fail_gpu = [RuntimeError("smi dead")] * 10
        wd = _wd(tel, log_path=tmp_path / "w.jsonl", startup_probe_attempts=3)
        assert wd.run() == FirstRunWatchdog.EXIT_ERROR
        evs = _events(tmp_path / "w.jsonl")
        assert evs[-1]["event"] == "refused" and "telemetry unavailable" in evs[-1]["reason"]
        assert tel.kill_calls == 0

    def test_refuses_below_readiness_vram_minimum(self, tmp_path):
        tel = FakeTelemetry()
        tel.gpu_seq = [4000.0]  # 3.9 GiB < 4.5 GiB readiness gate
        wd = _wd(tel, log_path=tmp_path / "w.jsonl")
        assert wd.run() == FirstRunWatchdog.EXIT_ERROR
        assert "insufficient free VRAM" in _events(tmp_path / "w.jsonl")[-1]["reason"]

    def test_refuses_below_readiness_ram_minimum(self, tmp_path):
        tel = FakeTelemetry()
        tel.ram_seq = [5000.0]  # ~4.9 GiB < 6 GiB readiness gate
        wd = _wd(tel, log_path=tmp_path / "w.jsonl")
        assert wd.run() == FirstRunWatchdog.EXIT_ERROR
        assert "insufficient available RAM" in _events(tmp_path / "w.jsonl")[-1]["reason"]

    def test_startup_probe_retries_then_succeeds(self, tmp_path):
        tel = FakeTelemetry()
        tel.fail_gpu = [RuntimeError("flaky")] * 2
        wd = _wd(tel, log_path=tmp_path / "w.jsonl", startup_probe_attempts=5)
        _on_sleep(wd, lambda n: tel.procs.clear() if n == 2 else None)
        # no worker → deadline eventually; use tiny deadline to exit fast
        wd.cfg.deadline_s = 4.0
        assert wd.run() == FirstRunWatchdog.EXIT_BREACH  # deadline, not refused
        evs = _events(tmp_path / "w.jsonl")
        assert sum(1 for e in evs if e["event"] == "startup_probe") == 2
        assert any(e["event"] == "ready" for e in evs)


# ------------------------------------------------------------ clean path

class TestCleanRun:
    def test_worker_comes_and_goes_cleanly(self, tmp_path):
        tel = FakeTelemetry()
        wd = _wd(tel, log_path=tmp_path / "w.jsonl")

        def hook(n):
            if n == 1:
                tel.add(100)
            elif n == 4:
                tel.procs.clear()
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_CLEAN
        assert tel.killed == []
        evs = _events(tmp_path / "w.jsonl")
        assert evs[-1]["event"] == "complete"
        assert all("ts" in e for e in evs)

    def test_no_worker_until_deadline_is_breach_not_hang(self):
        tel = FakeTelemetry()
        wd = _wd(tel, deadline_s=5.0, interval_s=1.0)
        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == []


# ------------------------------------------------------------ gauge breaches

class TestGaugeBreaches:
    def test_vram_breach_kills_worker_tree(self):
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100, children=[101])
                tel.add(101, name="child.exe", rss=10 * MIB, ctime=2.0)
            elif n == 2:
                tel.gpu_seq = [400.0]
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert sorted(tel.killed) == [100, 101]

    def test_single_vram_dip_does_not_kill(self):
        tel = FakeTelemetry()
        tel.gpu_seq = [7000.0, 400.0, 7000.0, 7000.0]
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100)
            elif n == 5:
                tel.procs.clear()
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_CLEAN
        assert tel.killed == []

    def test_ram_breach(self):
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100)
            elif n == 2:
                tel.ram_seq = [1000.0]
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [100]

    def test_tree_rss_breach(self):
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100, children=[101], rss=6 * 1024 * MIB)
                tel.add(101, name="child.exe", rss=3 * 1024 * MIB)
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert sorted(tel.killed) == [100, 101]


# ------------------------------------------------------------ structural

class TestStructuralBreaches:
    def test_deadline_kills_hung_worker(self):
        tel = FakeTelemetry()
        wd = _wd(tel, deadline_s=10.0, interval_s=1.0)
        _on_sleep(wd, lambda n: tel.add(100) if n == 1 else None)
        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [100]

    def test_multiple_workers_is_immediate_breach(self):
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100)
            elif n == 2:
                tel.add(200)
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert sorted(tel.killed) == [100, 200]

    def test_telemetry_loss_mid_run_fails_closed_and_kills(self):
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100)
            elif n == 2:
                tel.fail_gpu = [RuntimeError("smi dead")] * 5
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [100]


# ------------------------------------------------------------ identity / PID reuse

class TestPidIdentity:
    def test_reused_pid_is_never_killed_and_recorded(self, tmp_path):
        """A tracked pid whose create_time changed (OS reuse by an
        unrelated process) must survive and be reported."""
        tel = FakeTelemetry()
        wd = _wd(tel, log_path=tmp_path / "w.jsonl")
        calls = {"n": 0}

        def hook(n):
            calls["n"] = n
            if n == 1:
                tel.add(100, ctime=1.0)              # worker tracked
            elif n == 2:
                # force breach path while making rescan see nothing new
                tel.gpu_seq = [100.0]
            elif n == 3:
                # pid 100 reused by an UNRELATED process between last
                # healthy sample and the kill — identity now differs
                tel.procs[100]["ctime"] = 9.9
                tel.procs[100]["name"] = "unrelated.exe"
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert 100 in tel.mismatched and 100 not in tel.killed
        assert tel.identity_alive(100, 9.9)
        term = _events(tmp_path / "w.jsonl")[-1]
        assert term["identity_mismatch"] == [100]

    def test_reused_child_pid_skipped_but_parent_killed(self):
        """Identity check applies per-process: the stale child is
        skipped, the still-matching parent is terminated."""
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100, children=[101], ctime=1.0)
                tel.add(101, name="child.exe", ctime=5.0)
            elif n == 2:
                tel.gpu_seq = [100.0]
            elif n == 3:
                # child pid reused by a DETACHED unrelated process: no
                # longer in the worker's children, so the rescan cannot
                # refresh its identity — the stale record must mismatch.
                tel.procs[100]["children"] = []
                tel.procs[101]["ctime"] = 7.7
                tel.procs[101]["name"] = "other.exe"
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [100] and tel.mismatched == [101]
        assert tel.identity_alive(101, 7.7)

    def test_baseline_pid_reuse_counts_as_new_worker(self):
        """A baseline pid REUSED by a fresh sd-server.exe is a NEW
        identity — it is post-baseline and must be guarded like any
        worker."""
        tel = FakeTelemetry()
        tel.add(50, ctime=1.0)                    # foreign worker at start
        wd = _wd(tel, allow_baseline=True, deadline_s=8.0)

        def hook(n):
            if n == 1:
                del tel.procs[50]                  # old process exits
                tel.add(50, ctime=2.0)             # OS reuses the pid
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [50]                  # the NEW process, not the old


# ------------------------------------------------------------ targeting safety

class TestKillTargeting:
    def test_baseline_processes_are_never_killed(self, tmp_path):
        tel = FakeTelemetry()
        tel.add(50)
        wd = _wd(tel, allow_baseline=True, log_path=tmp_path / "w.jsonl")

        def hook(n):
            if n == 1:
                tel.add(100)
                tel.gpu_seq = [100.0]
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [100]
        assert tel.identity_alive(50, 1.0)
        term = _events(tmp_path / "w.jsonl")[-1]
        assert term["targets"] == [100]

    def test_orphaned_children_after_worker_exit_are_killed(self):
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100, children=[101])
                tel.add(101, name="helper.exe", rss=64 * MIB)
            elif n == 3:
                del tel.procs[100]
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [101]

    def test_dry_run_records_but_does_not_kill(self, tmp_path):
        tel = FakeTelemetry()
        wd = _wd(tel, kill_enabled=False, deadline_s=3.0,
                 log_path=tmp_path / "w.jsonl")
        _on_sleep(wd, lambda n: tel.add(100) if n == 1 else None)
        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.kill_calls == 0
        term = _events(tmp_path / "w.jsonl")[-1]
        assert term["dry_run"] is True and term["targets"] == [100]


# ------------------------------------------------------------ real processes

@pytest.mark.skipif(sys.platform != "win32", reason="Windows tree kill")
class TestRealProcessTree:
    def test_real_tree_enumeration_and_identity_checked_kill(self):
        """Synthetic parent python proc + child: tree_snaps must return
        identity-bearing snaps and kill_tree reaps only matching pids."""
        tel = PsutilTelemetry()
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess,sys,time;"
             "subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);"
             "time.sleep(300)"])
        try:
            deadline = time.monotonic() + 10
            snaps: list[ProcSnap] = []
            root = ProcSnap(pid=parent.pid, name="python.exe",
                            create_time=tel._psutil.Process(parent.pid).create_time())
            while time.monotonic() < deadline:
                snaps = tel.tree_snaps(root)
                if len(snaps) >= 2:
                    break
                time.sleep(0.1)
            assert len(snaps) >= 2, f"child never appeared: {snaps}"
            assert all(s.create_time > 0 for s in snaps)
            rep = tel.kill_tree(snaps)
            assert rep.survived == [] and rep.identity_mismatch == []
            for s in snaps:
                assert not tel.identity_alive(s.pid, s.create_time)
        finally:
            try:
                parent.kill()
            except OSError:
                pass

    def test_kill_tree_skips_a_reused_pid(self):
        """Identity check on real processes: a snap whose create_time
        no longer matches the live process must not be signaled."""
        tel = PsutilTelemetry()
        proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        try:
            stale = ProcSnap(pid=proc.pid, name="python.exe",
                             create_time=time.time() + 9999)  # wrong identity
            rep = tel.kill_tree([stale])
            assert rep.identity_mismatch == [proc.pid] and rep.killed == []
            assert proc.poll() is None  # still alive — never signaled
        finally:
            proc.kill()
