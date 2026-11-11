"""Deterministic tests for the first-run inference watchdog.

Telemetry and the clock are faked — no real GPU, RAM pressure, model
weights, or inference. The only real-OS test spawns synthetic Python
process trees to verify tree-RSS accounting and tree termination.

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
    FirstRunWatchdog, ProcSnap, PsutilTelemetry, WatchdogConfig)

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
    """Scripted telemetry. `procs` maps pid -> {name, children, rss};
    every key present is alive. RAM/GPU readings pop from sequences
    (the last value repeats)."""

    def __init__(self) -> None:
        self.procs: dict[int, dict] = {}
        self.ram_seq: list[float] = [8192.0]
        self.gpu_seq: list[float] = [7000.0]
        self.fail_gpu: list[Exception] = []
        self.fail_procs = 0
        self.killed: list[int] = []
        self.kill_calls = 0

    def _next(self, seq: list[float]) -> float:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def ram_available_mib(self) -> float:
        return self._next(self.ram_seq)

    def gpu_free_mib(self, index: int) -> float:
        if self.fail_gpu:
            raise self.fail_gpu.pop(0)
        return self._next(self.gpu_seq)

    def worker_pids(self, name: str) -> list[ProcSnap]:
        if self.fail_procs > 0:
            self.fail_procs -= 1
            raise RuntimeError("process enumeration failed")
        return [ProcSnap(pid=p, name=d["name"]) for p, d in self.procs.items()
                if d["name"].lower() == name.lower()]

    def tree_pids(self, root_pid: int) -> list[int]:
        if root_pid not in self.procs:
            return []
        out = [root_pid]
        for c in self.procs[root_pid]["children"]:
            if c in self.procs:
                out.append(c)
                out.extend(x for x in self.tree_pids(c) if x not in out)
        return out

    def rss_bytes(self, pids) -> int:
        return sum(self.procs[p]["rss"] for p in pids if p in self.procs)

    def alive(self, pid: int) -> bool:
        return pid in self.procs

    def kill_tree(self, pids) -> list[int]:
        self.kill_calls += 1
        for p in pids:
            if p in self.procs:
                self.killed.append(p)
                del self.procs[p]
        return []

    def add(self, pid: int, name: str = WORKER, children=(), rss: int = 512 * MIB):
        self.procs[pid] = {"name": name, "children": list(children), "rss": rss}


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


# ------------------------------------------------------------ clean path

class TestCleanRun:
    def test_worker_comes_and_goes_cleanly(self, tmp_path):
        tel = FakeTelemetry()
        wd = _wd(tel, log_path=tmp_path / "w.jsonl")

        def hook(n):
            if n == 1:
                tel.add(100)            # worker spawns after baseline
            elif n == 4:
                tel.procs.clear()       # worker exits cleanly
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_CLEAN
        assert tel.killed == []
        lines = [json.loads(l) for l in (tmp_path / "w.jsonl").read_text().splitlines()]
        events = [l["event"] for l in lines]
        assert events[0] == "start" and "sample" in events and events[-1] == "complete"
        assert all("ts" in l for l in lines)

    def test_no_worker_until_deadline_is_breach_not_hang(self):
        tel = FakeTelemetry()
        wd = _wd(tel, deadline_s=5.0, interval_s=1.0)
        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == []  # nothing to kill


# ------------------------------------------------------------ gauge breaches

class TestGaugeBreaches:
    def test_vram_breach_kills_worker_tree(self):
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100, children=[101])
                tel.add(101, name="child.exe", rss=10 * MIB)
            elif n == 2:
                tel.gpu_seq = [400.0]   # sustained low VRAM
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert sorted(tel.killed) == [100, 101]  # whole tree, not just root

    def test_single_vram_dip_does_not_kill(self):
        tel = FakeTelemetry()
        tel.gpu_seq = [7000.0, 400.0, 7000.0, 7000.0]  # one bad sample only
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
                tel.ram_seq = [1000.0]  # below 1536 floor, sustained
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [100]

    def test_tree_rss_breach(self):
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100, children=[101], rss=6 * 1024 * MIB)
                tel.add(101, name="child.exe", rss=3 * 1024 * MIB)  # 9 GiB > 8
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
                tel.add(200)  # second unexpected sd-server
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert sorted(tel.killed) == [100, 200]

    def test_telemetry_loss_fails_closed(self):
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

    def test_telemetry_dead_from_launch_still_kills_discovered_worker(self):
        """If telemetry dies before ANY healthy sample, _tracked_tree is
        empty — the final re-scan must still find and kill the worker."""
        tel = FakeTelemetry()
        tel.fail_gpu = [RuntimeError("smi dead")] * 5  # dead from the start
        wd = _wd(tel)
        _on_sleep(wd, lambda n: tel.add(100) if n == 1 else None)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [100]


# ------------------------------------------------------------ targeting safety

class TestKillTargeting:
    def test_baseline_processes_are_never_killed(self, tmp_path):
        tel = FakeTelemetry()
        tel.add(50)   # foreign sd-server already running at baseline
        wd = _wd(tel, log_path=tmp_path / "w.jsonl")

        def hook(n):
            if n == 1:
                tel.add(100)          # our worker appears post-baseline
                tel.gpu_seq = [100.0]  # force breach
        _on_sleep(wd, hook)

        assert wd.run() == FirstRunWatchdog.EXIT_BREACH
        assert tel.killed == [100]     # baseline pid 50 untouched
        assert tel.alive(50)
        term = json.loads((tmp_path / "w.jsonl").read_text().splitlines()[-1])
        assert term["event"] == "termination" and term["targets"] == [100]

    def test_orphaned_children_after_worker_exit_are_killed(self):
        tel = FakeTelemetry()
        wd = _wd(tel)

        def hook(n):
            if n == 1:
                tel.add(100, children=[101])
                tel.add(101, name="helper.exe", rss=64 * MIB)
            elif n == 3:
                del tel.procs[100]     # parent gone, child survives
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
        term = json.loads((tmp_path / "w.jsonl").read_text().splitlines()[-1])
        assert term["dry_run"] is True and term["targets"] == [100]


# ------------------------------------------------------------ real processes

@pytest.mark.skipif(sys.platform != "win32", reason="Windows tree kill")
class TestRealProcessTree:
    def test_real_tree_enumeration_and_kill(self):
        """Synthetic parent python proc spawning a child; verify
        tree_pids sees both and kill_tree reaps the whole tree."""
        tel = PsutilTelemetry()
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess,sys,time;"
             "subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);"
             "time.sleep(300)"])
        try:
            deadline = time.monotonic() + 10
            tree = []
            while time.monotonic() < deadline:
                tree = tel.tree_pids(parent.pid)
                if len(tree) >= 2:
                    break
                time.sleep(0.1)
            assert len(tree) >= 2, f"child never appeared: {tree}"
            assert tel.rss_bytes(tree) > 0
            assert tel.kill_tree(tree) == []
            for pid in tree:
                assert not tel.alive(pid)
        finally:
            try:
                parent.kill()
            except OSError:
                pass
