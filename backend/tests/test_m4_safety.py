"""M4.0 safety: worker lifecycle, memory preflight, pressure watchdog.

All tests are simulated/bounded — no real inference, no memory stress
(issue #28 staging).
"""
import os
import subprocess
import sys
import time

import pytest

from app.domain import proc_guard, resources


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_job_object_kills_worker_when_handle_closed():
    """The OS-level orphan fix: closing the job handle must kill the worker
    even if the owner 'dies' (simulated by dropping the handle)."""
    p = _spawn_sleeper()
    try:
        proc_guard.supervise(p.pid)
        assert p.pid in proc_guard._live_workers
        assert proc_guard.process_alive(p.pid)
        # simulate backend death: the job handle is the only thing keeping
        # the worker's membership; closing it must reap the process
        proc_guard.unsupervise(p.pid)
        deadline = time.time() + 10
        while time.time() < deadline and proc_guard.process_alive(p.pid):
            time.sleep(0.2)
        assert not proc_guard.process_alive(p.pid), \
            "worker survived kill-on-close — orphan risk remains"
    finally:
        if p.poll() is None:
            p.kill()


def test_terminate_tree_kills_stubborn_process():
    p = _spawn_sleeper()
    proc_guard.terminate_tree(p.pid, grace_s=2.0)
    p.wait(timeout=10)
    assert not proc_guard.process_alive(p.pid)


def test_supervise_unsupervise_lifecycle():
    p = _spawn_sleeper()
    try:
        proc_guard.supervise(p.pid)
        proc_guard.unsupervise(p.pid)
        assert p.pid not in proc_guard._live_workers
        assert p.pid not in proc_guard._job_handles
    finally:
        p.kill()


def test_process_rss_bytes_reports_memory():
    p = _spawn_sleeper()
    try:
        rss = proc_guard.process_rss_bytes(p.pid)
        assert rss is None or rss > 1_000_000  # any live python has MBs of RSS
    finally:
        p.kill()


def test_system_memory_shape():
    m = resources.system_memory()
    assert m["total_phys_bytes"] > 0
    assert m["avail_phys_bytes"] >= 0
    assert m["commit_headroom_bytes"] >= 0


def test_preflight_blocks_when_ram_insufficient(monkeypatch):
    monkeypatch.setattr(resources, "system_memory", lambda: {
        "total_phys_bytes": 16 * resources.GIB,
        "avail_phys_bytes": int(6 * resources.GIB),
        "phys_load_pct": 60, "commit_limit_bytes": 40 * resources.GIB,
        "commit_charge_bytes": 10 * resources.GIB,
        "commit_headroom_bytes": 30 * resources.GIB})
    # Wan-class need: ~6.3 GiB weights + 2 GiB overhead on 6 GiB free
    out = resources.preflight_memory_check(int(8.3 * resources.GIB), known=True)
    assert out["decision"] == "block"
    assert "insufficient system RAM" in out["reason"]


def test_preflight_warns_on_tight_headroom(monkeypatch):
    monkeypatch.setattr(resources, "system_memory", lambda: {
        "total_phys_bytes": 16 * resources.GIB,
        "avail_phys_bytes": int(11 * resources.GIB),
        "phys_load_pct": 35, "commit_limit_bytes": 40 * resources.GIB,
        "commit_charge_bytes": 5 * resources.GIB,
        "commit_headroom_bytes": 35 * resources.GIB})
    out = resources.preflight_memory_check(int(8.3 * resources.GIB), known=True)
    assert out["decision"] == "warn"  # 2.7 GiB after < 4 GiB OS headroom


def test_preflight_unknown_estimate_warns():
    out = resources.preflight_memory_check(0, known=False)
    assert out["decision"] == "warn"
    assert "unknown" in out["reason"]


def test_pressure_state_thresholds(monkeypatch):
    monkeypatch.setattr(resources, "system_memory", lambda: {
        "total_phys_bytes": 16 * resources.GIB,
        "avail_phys_bytes": int(0.5 * resources.GIB),  # < critical 1 GiB
        "phys_load_pct": 98, "commit_limit_bytes": 40 * resources.GIB,
        "commit_charge_bytes": 15 * resources.GIB,
        "commit_headroom_bytes": 25 * resources.GIB})
    assert resources.pressure_state() == "critical"


def test_worker_pressure_abort_reason(monkeypatch):
    from app.adapters.inference import _check_worker_pressure
    monkeypatch.setattr(resources, "pressure_state", lambda: "critical")
    reason = _check_worker_pressure(object(), time.time())
    assert "memory pressure critical" in reason


def test_worker_runtime_cap(monkeypatch):
    from app.adapters.inference import _check_worker_pressure
    monkeypatch.setattr(resources, "pressure_state", lambda: "ok")
    monkeypatch.setenv("ATOMIC_MAX_WORKER_SECONDS", "10")
    reason = _check_worker_pressure(object(), time.time() - 20)
    assert "runtime cap" in reason


def test_memory_preflight_uses_offloaded_weights():
    from app.domain.jobs import _memory_preflight
    from app.adapters.base import CapabilityProfile
    prof = CapabilityProfile(adapter_id="gen.video.sdcpp", display_name="x",
                             adapter_kind="local", tasks=("text2video",), lane="gen")
    out = _memory_preflight({"model_key": "wan21.t2v.1_3b"}, prof)
    snap = out["snapshot"]
    # Wan declares weight_offload_cpu → full 6.28 GiB weights counted
    assert snap["estimated_ram_bytes"] > 8 * resources.GIB
    assert snap["estimate_known"] is True
    assert out["decision"] in ("ok", "warn", "block")


def test_compatibility_gates_on_available_ram():
    from app.domain import compatibility, model_catalog
    spec = model_catalog.get("wan21.t2v.1_3b")
    hw = {
        "backends": {"vulkan": {"available": True}, "cuda": {"available": True},
                     "cpu": {"available": True}},
        "gpus": [{"index": 0, "vram_bytes": 8 * 1024**3, "vram_free_bytes": 7 * 1024**3}],
        "memory": {"total_bytes": 16 * 1024**3, "available_bytes": int(2 * 1024**3)},
        "cpu": {"available": True},
    }
    c = compatibility.classify_model(spec, hw)
    assert c["status"] == "unsupported"
    assert any("available system RAM" in r for r in c["reasons"])


# ---------- Stage 3A: real supervised-process validation (bounded) ----------

SLEEPER = [sys.executable, "-c", "import time; time.sleep(300)"]

HELPER_SRC = (
    "import sys; sys.path.insert(0, {backend!r});"
    "from app.domain import proc_guard;"
    "p = proc_guard.spawn_supervised({sleeper!r});"
    "open({pidfile!r}, 'w').write(str(p.pid));"
    "import time; time.sleep(3600)")


def test_spawn_supervised_normal_exit():
    p = proc_guard.spawn_supervised([sys.executable, "-c", "print('hi')"])
    assert p.wait(timeout=15) == 0
    proc_guard.unsupervise(p.pid)
    assert p.pid not in proc_guard._live_workers


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only supervision")
def test_spawn_supervised_forced_termination():
    p = proc_guard.spawn_supervised(SLEEPER)
    assert proc_guard.process_alive(p.pid)
    proc_guard.terminate_tree(p.pid, grace_s=2.0)
    proc_guard.unsupervise(p.pid)
    deadline = time.time() + 10
    while time.time() < deadline and proc_guard.process_alive(p.pid):
        time.sleep(0.2)
    assert not proc_guard.process_alive(p.pid)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only supervision")
def test_backend_death_reaps_worker(tmp_path):
    """Simulate `taskkill /F` on the backend: the worker must die because
    the kernel closes the last job-object handle — the orphan fix."""
    pidfile = tmp_path / "worker.pid"
    helper = subprocess.Popen(
        [sys.executable, "-c",
         HELPER_SRC.format(backend=str(os.getcwd()).replace("\\", "/"),
                          sleeper=SLEEPER, pidfile=str(pidfile).replace("\\", "/"))],
        cwd=os.getcwd())
    try:
        # wait for the helper to spawn + supervise the worker
        worker_pid = None
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                worker_pid = int(pidfile.read_text().strip()); break
            time.sleep(0.1)
        assert worker_pid, "helper never reported a worker pid"
        assert proc_guard.process_alive(worker_pid)
        # hard-kill the 'backend' — no graceful shutdown, like taskkill /F
        subprocess.run(["taskkill", "/F", "/PID", str(helper.pid)],
                       capture_output=True)
        deadline = time.time() + 15
        while time.time() < deadline and proc_guard.process_alive(worker_pid):
            time.sleep(0.25)
        assert not proc_guard.process_alive(worker_pid), \
            "worker survived backend force-kill — kill-on-close failed"
    finally:
        if helper.poll() is None:
            helper.kill()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only supervision")
def test_supervised_spawn_fail_closed(monkeypatch):
    """If the job object can't be created, the worker must NEVER run."""
    monkeypatch.setattr(proc_guard, "_create_kill_on_close_job", lambda: None)
    with pytest.raises(proc_guard.SupervisionError):
        proc_guard.spawn_supervised(SLEEPER)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only supervision")
def test_repeated_cycles_no_handle_leak():
    base = len(proc_guard._job_handles)
    for _ in range(8):
        p = proc_guard.spawn_supervised([sys.executable, "-c", "pass"])
        p.wait(timeout=15)
        proc_guard.unsupervise(p.pid)
    assert len(proc_guard._job_handles) == base
    assert not proc_guard._live_workers


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only supervision")
def test_ram_measurements_around_worker():
    before = resources.system_memory()["avail_phys_bytes"]
    p = proc_guard.spawn_supervised(SLEEPER)
    time.sleep(0.5)
    rss = proc_guard.process_rss_bytes(p.pid)
    proc_guard.terminate_tree(p.pid, grace_s=2.0)
    proc_guard.unsupervise(p.pid)
    after = resources.system_memory()["avail_phys_bytes"]
    # a tiny python sleeper: RSS measurable, machine-level drift small
    assert rss is None or rss > 1_000_000
    assert abs(after - before) < 2 * resources.GIB
