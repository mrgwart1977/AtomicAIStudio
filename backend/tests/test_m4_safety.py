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
