"""A7 — multi-GPU scheduling + provider-neutral cloud gates.

Scheduler tests use deterministic dict probes (simulated devices —
never real hardware). Engine tests run the real claim/release path
through mock.gpu.sim: deterministic in-process simulated work, zero
inference. Cloud-plan tests compose provider capability + budget +
credential-presence + availability with mock providers only — no live
provider calls anywhere in this file.
"""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview

GIB = 2 ** 30


def _hw(vrams, *, backends=None):
    """Simulated hardware probe report."""
    return {
        "gpus": [
            {"index": i, "name": f"SIM{i}", "vendor": "NVIDIA",
             "vram_bytes": int(v * GIB), "vram_free_bytes": int(v * GIB)}
            for i, v in enumerate(vrams)
        ],
        "backends": backends or {
            "cuda": {"available": True},
            "vulkan": {"available": True},
            "directml": {"available": True},
            "cpu": {"available": True},
        },
    }


def _sched(hw, prefs=None):
    from app.domain.gpu_sched import GpuScheduler
    return GpuScheduler(hw_probe=hw, prefs=prefs or {})


def _use_sim_gpus(client, vrams, *, prefs=None, backends=None):
    """Point the running engine + scheduler state at simulated devices."""
    sched = _sched(_hw(vrams, backends=backends), prefs=prefs)
    client.app.state.job_engine._sched = sched
    client.app.state.gpu_scheduler = sched
    return sched


def _sim_job(client, pid, payload=None, expect=200, key=None):
    return _commit(client, pid, "sim.gpu.work", payload or {},
                   expect=expect, key=key)


def _job(client, pid, job_id):
    return next(j for j in client.get(
        f"/api/projects/{pid}/jobs").json() if j["id"] == job_id)


# ---------------------------------------------------------------- unit

def test_single_gpu_shared_reservations():
    """Two small jobs share one device by reservation accounting; the
    third that would oversubscribe stays queued with a reason."""
    s = _sched(_hw([8]))
    assert s.acquire("j1", {}, est_vram=3 * GIB) == 0
    assert s.acquire("j2", {}, est_vram=4 * GIB) == 0
    assert s.acquire("j3", {}, est_vram=4 * GIB) is None
    assert "fits" in s.last_reason or "busy" in s.last_reason
    s.release("j1")
    assert s.acquire("j3", {}, est_vram=4 * GIB) == 0


def test_multi_gpu_spread_and_accounting():
    s = _sched(_hw([4, 8]))
    # 6 GiB job → only GPU 1 fits.
    assert s.acquire("big", {}, est_vram=6 * GIB) == 1
    # Balanced selection: GPU 0 (4 free) wins over GPU 1 (2 free) —
    # small jobs spread rather than packing one device.
    assert s.acquire("s1", {}, est_vram=2 * GIB) == 0
    st = {sl["index"]: sl for sl in s.slots()}
    assert st[1]["jobs"] == {"big": 6 * GIB}
    assert st[0]["jobs"] == {"s1": 2 * GIB}
    # Both devices now offer 2 GiB — the next small job lands on
    # whichever the scorer picks.
    assert s.acquire("s2", {}, est_vram=2 * GIB) in (0, 1)
    # But a job exceeding every device's remaining space is refused.
    assert s.acquire("s3", {}, est_vram=4 * GIB) is None
    assert "fits" in s.last_reason


def test_unknown_footprint_claims_device_exclusively():
    """A job with no estimate can never gamble — it takes the whole
    device, and nothing else shares until it releases."""
    s = _sched(_hw([8, 8]))
    assert s.acquire("mystery", {}, est_vram=None) == 0
    assert s.acquire("small", {}, est_vram=GIB) == 1
    s.release("mystery")
    assert s.acquire("small2", {}, est_vram=GIB) == 0


def test_oversubscription_refused_with_reason():
    s = _sched(_hw([4]))
    assert s.acquire("huge", {}, est_vram=12 * GIB) is None
    assert "no device fits" in s.last_reason
    # Reservations unchanged — a refused admit holds nothing.
    assert s.reservations() == {}


def test_pinned_device_and_busy_reason():
    s = _sched(_hw([4, 8]))
    assert s.acquire("j1", {"device": 1}, est_vram=6 * GIB) == 1
    # Pinned to a device that can't admit — no silent migration.
    assert s.acquire("j2", {"device": 1}, est_vram=6 * GIB) is None
    assert "GPU 1" in s.last_reason
    # Missing index.
    assert s.acquire("j3", {"device": 9}) is None
    assert "not present" in s.last_reason


def test_backend_capability_matching():
    """'cuda' needs an NVIDIA device; a vendor mismatch or an
    unavailable backend refuses with the unmet requirement."""
    hw = _hw([8, 8])
    hw["gpus"][1]["vendor"] = "AMD"
    s = _sched(hw)
    assert s.acquire("j1", {}, est_vram=2 * GIB,
                     requires=["cuda"]) == 0  # NVIDIA device picked
    hw2 = _hw([8], backends={"cuda": {"available": False},
                             "vulkan": {"available": True},
                             "cpu": {"available": True}})
    s2 = _sched(hw2)
    assert s2.acquire("j2", {}, requires=["cuda"]) is None
    assert "cuda" in (s2.last_reason or "")
    # The same request admits on vulkan when required to.
    assert s2.acquire("j3", {}, requires=["vulkan"]) == 0


def test_device_disappearance_is_honest():
    """A device that vanishes while holding a reservation is reported
    lost — never silently freed, never schedulable."""
    hw = _hw([8, 8])
    s = _sched(hw)
    assert s.acquire("j1", {"device": 1}, est_vram=4 * GIB) == 1
    s._probe = _hw([8])  # device 1 disappears
    assert s.lost_devices() == [1]
    slot1 = next(sl for sl in s.slots() if sl["index"] == 1)
    assert slot1["present"] is False
    assert slot1["jobs"] == {"j1": 4 * GIB}
    assert s.acquire("j2", {"device": 1}) is None
    assert "not present" in s.last_reason


def test_release_on_all_paths_frees_capacity():
    s = _sched(_hw([4]))
    s.acquire("j1", {}, est_vram=4 * GIB)
    s.release("j1")
    s.release("j1")  # double-release is a no-op
    assert s.acquire("j2", {}, est_vram=4 * GIB) == 0
    assert s.slots()[0]["jobs"] == {"j2": 4 * GIB}


def test_small_jobs_not_starved_by_big_waiter():
    """A big job that can't be admitted does not starve smaller jobs —
    they land on whichever device can take them."""
    s = _sched(_hw([4, 4]))
    s.acquire("hold0", {"device": 0}, est_vram=3 * GIB)
    s.acquire("hold1", {"device": 1}, est_vram=3 * GIB)
    # Big job: nowhere fits right now.
    assert s.acquire("big", {}, est_vram=3 * GIB) is None
    # Small job still admitted on either device's remaining GiB.
    assert s.acquire("small", {}, est_vram=GIB) in (0, 1)


# ------------------------------------------------------- engine paths

def test_sim_job_runs_on_single_gpu_and_releases(client):
    _use_sim_gpus(client, [8])
    pid = make_project(client)["id"]
    r = _sim_job(client, pid, {"est_vram_bytes": 2 * GIB,
                             "work_ms": 150})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded"
    assert job["gpu_index"] == 0
    assert job["reserved_vram_bytes"] == 2 * GIB
    # Released after success — the device admits a new exclusive claim.
    sched = client.app.state.job_engine._sched
    assert sched.slots()[0]["jobs"] == {}


def test_multi_gpu_concurrent_jobs(client):
    _use_sim_gpus(client, [8, 8])
    pid = make_project(client)["id"]
    r1 = _sim_job(client, pid, {"est_vram_bytes": 6 * GIB,
                               "work_ms": 900})
    r2 = _sim_job(client, pid, {"est_vram_bytes": 6 * GIB,
                               "work_ms": 900})
    j1 = wait_for_job(client, pid, r1["job_id"], timeout=30)
    j2 = wait_for_job(client, pid, r2["job_id"], timeout=30)
    assert j1["status"] == j2["status"] == "succeeded"
    assert {j1["gpu_index"], j2["gpu_index"]} == {0, 1}


def test_oversubscribed_job_queues_then_admits(client):
    _use_sim_gpus(client, [4])
    pid = make_project(client)["id"]
    r1 = _sim_job(client, pid, {"est_vram_bytes": 3 * GIB,
                               "work_ms": 1200})
    j1id = r1["job_id"]
    # Second job can't fit alongside (4-3=1 free < 3 needed) → queued.
    r2 = _sim_job(client, pid, {"est_vram_bytes": 3 * GIB,
                               "work_ms": 50})
    j2id = r2["job_id"]
    j1 = wait_for_job(client, pid, j1id, timeout=30)
    assert j1["status"] == "succeeded"
    j2 = wait_for_job(client, pid, j2id, timeout=30)
    assert j2["status"] == "succeeded"
    assert j2["gpu_index"] == 0  # admitted after release, not dropped


def test_cancelled_job_releases_reservation(client):
    _use_sim_gpus(client, [4])
    pid = make_project(client)["id"]
    r = _sim_job(client, pid, {"est_vram_bytes": 3 * GIB,
                              "work_ms": 8000})
    jid = r["job_id"]
    # Wait until running then cancel.
    import time
    for _ in range(100):
        j = _job(client, pid, jid)
        if j["status"] == "running":
            break
        time.sleep(0.05)
    client.post(f"/api/jobs/{jid}/cancel")
    j = wait_for_job(client, pid, jid, timeout=30)
    assert j["status"] == "cancelled"
    assert client.app.state.job_engine._sched.slots()[0]["jobs"] == {}


def test_worker_crash_fails_job_and_releases(client):
    _use_sim_gpus(client, [4])
    pid = make_project(client)["id"]
    r = _sim_job(client, pid, {"est_vram_bytes": 2 * GIB,
                              "work_ms": 8000, "crash_after_ms": 80})
    j = wait_for_job(client, pid, r["job_id"], timeout=30)
    assert j["status"] == "failed"
    assert "crash" in (j["error"] or "")
    assert client.app.state.job_engine._sched.slots()[0]["jobs"] == {}


def test_restart_reconcile_marks_interrupted_terminal(client):
    """sim.gpu.work follows the generation contract: non-resumable —
    a job left 'running' reconciles to failed/interrupted, never
    silently resumed. The live task's reservation stays held until
    the task itself exits (honest accounting, no phantom free)."""
    _use_sim_gpus(client, [4])
    pid = make_project(client)["id"]
    r = _sim_job(client, pid, {"est_vram_bytes": 2 * GIB,
                              "work_ms": 60000})  # parked long-running
    jid = r["job_id"]
    import time
    for _ in range(100):
        if _job(client, pid, jid)["status"] == "running":
            break
        time.sleep(0.05)
    eng = client.app.state.job_engine
    with client.app.state.session_factory() as s:
        n = eng.reconcile_interrupted(s)
        s.commit()
        assert n == 0  # non-resumable → failed, not re-queued
    j = _job(client, pid, jid)
    assert j["status"] == "failed"
    assert j["recovery_state"] == "interrupted"
    # The task that still holds the reservation keeps it accounted —
    # reconcile never frees capacity of live work.
    slot0 = client.app.state.job_engine._sched.slots()[0]
    assert jid in slot0["jobs"]
    # Cancel the parked task so teardown isn't waiting on 60 s.
    client.post(f"/api/jobs/{jid}/cancel")


def test_pinned_job_uses_requested_device(client):
    _use_sim_gpus(client, [4, 4])
    pid = make_project(client)["id"]
    r = _sim_job(client, pid, {"device": 1,
                             "est_vram_bytes": 2 * GIB,
                             "work_ms": 150})
    j = wait_for_job(client, pid, r["job_id"])
    assert j["gpu_index"] == 1


def test_cpu_lane_never_touches_gpu(client):
    _use_sim_gpus(client, [4])
    pid = make_project(client)["id"]
    r = _sim_job(client, pid, {"device": "cpu", "work_ms": 150})
    j = wait_for_job(client, pid, r["job_id"])
    assert j["status"] == "succeeded"
    assert j["gpu_index"] is None
    assert client.app.state.job_engine._sched.slots()[0]["jobs"] == {}


def test_sim_preview_shows_admission_decision(client):
    _use_sim_gpus(client, [4])
    pid = make_project(client)["id"]
    pv = _preview(client, pid, "sim.gpu.work",
                  {"est_vram_bytes": 2 * GIB})
    rg = pv["resolved_gpu"]
    assert rg["would_schedule"] is True and rg["device"] == 0
    # Oversubscribed preview reports the queue reason honestly.
    pv2 = _preview(client, pid, "sim.gpu.work",
                   {"est_vram_bytes": 9 * GIB})
    assert pv2["resolved_gpu"]["would_schedule"] is False
    assert pv2["resolved_gpu"]["queue_reason"]


def test_scheduler_api_reports_reservations(client):
    _use_sim_gpus(client, [8, 8])
    pid = make_project(client)["id"]
    r = _sim_job(client, pid, {"device": 0,
                              "est_vram_bytes": 3 * GIB,
                              "work_ms": 3000})
    import time
    for _ in range(100):
        if _job(client, pid, r["job_id"])["status"] == "running":
            break
        time.sleep(0.05)
    st = client.get("/api/gpu/scheduler").json()
    slot0 = next(s for s in st["slots"] if s["index"] == 0)
    assert slot0["reserved_bytes"] == 3 * GIB
    assert r["job_id"] in slot0["jobs"]
    wait_for_job(client, pid, r["job_id"], timeout=30)


# -------------------------------------------------------- cloud plan

def _enable(client, key):
    r = client.put(f"/api/providers/{key}/config",
                   json={"enabled": True})
    assert r.status_code == 200, r.text


def test_cloud_plan_preview_shape(client):
    pid = make_project(client)["id"]
    _enable(client, "sudowrite")
    pv = _preview(client, pid, "cloud.plan",
                  {"provider_key": "sudowrite", "task": "text",
                   "est_cost_usd": 0.25})
    rc = pv["resolved_cloud"]
    assert rc["ok"] and rc["provider_key"] == "sudowrite"
    assert rc["environment"] == "simulated"  # mock adapter booking env
    assert rc["plan_fingerprint"]
    assert "credential" in rc or rc["credential_required"] is False
    assert rc["simulated"] is True
    # Budget rows are composed per scope.
    assert {b["scope"] for b in rc["budgets"]} == {
        "session", "day", "month", "lifetime"}


def test_cloud_plan_commit_records_approval(client):
    pid = make_project(client)["id"]
    _enable(client, "sudowrite")
    pv = _preview(client, pid, "cloud.plan",
                  {"provider_key": "sudowrite", "task": "text"})
    fp = pv["resolved_cloud"]["plan_fingerprint"]
    r = _commit(client, pid, "cloud.plan",
                {"provider_key": "sudowrite", "task": "text",
                 "expected_plan_fingerprint": fp})
    assert r["status"] == "committed"


def test_cloud_plan_stale_fingerprint_refused(client):
    pid = make_project(client)["id"]
    _enable(client, "sudowrite")
    r = _commit(client, pid, "cloud.plan",
                {"provider_key": "sudowrite", "task": "text",
                 "expected_plan_fingerprint": "0" * 64},
                expect=422)
    assert "plan" in r.text


def test_cloud_plan_disabled_provider_refused(client):
    pid = make_project(client)["id"]
    pv = _preview(client, pid, "cloud.plan",
                  {"provider_key": "sudowrite", "task": "text"})
    rc = pv["resolved_cloud"]
    assert rc["availability"] == "disabled"
    assert rc["would_admit"] is False
    _commit(client, pid, "cloud.plan",
            {"provider_key": "sudowrite", "task": "text"}, expect=422)


def test_cloud_plan_budget_refusal(client):
    pid = make_project(client)["id"]
    _enable(client, "sudowrite")
    _commit(client, pid, "spend.budget_set",
            {"target": "project", "scope": "month", "limit_usd": 0.10})
    pv = _preview(client, pid, "cloud.plan",
                  {"provider_key": "sudowrite", "task": "text",
                   "est_cost_usd": 5.0})
    rc = pv["resolved_cloud"]
    assert rc["availability"] == "over_budget"
    assert "month" in rc["over_budget"]
    _commit(client, pid, "cloud.plan",
            {"provider_key": "sudowrite", "task": "text",
             "est_cost_usd": 5.0}, expect=422)


def test_cloud_plan_needs_key_availability(client):
    """A credential-requiring provider without a configured credential
    reports blocked — presence only, never a secret value."""
    pid = make_project(client)["id"]
    _enable(client, "openai")
    pv = _preview(client, pid, "cloud.plan",
                  {"provider_key": "openai", "task": "text"})
    rc = pv["resolved_cloud"]
    assert rc["availability"] in ("blocked", "disabled")
    if rc.get("credential"):
        assert set(rc["credential"]) <= {
            "configured", "fields", "unreadable"}
        # fields are names, never values.
        assert all(isinstance(f, str) for f in rc["credential"]["fields"])


def test_submit_plan_fingerprint_pin(client):
    """A cloud submit pinning a plan fingerprint refuses when the
    pinned plan no longer matches — explicit per-job approval.
    (rytr: T1-eligible, routes to the in-memory mock adapter.)"""
    pid = make_project(client)["id"]
    _enable(client, "rytr")
    # Bogus fingerprint refuses before any job exists.
    r = _commit(client, pid, "generate.text.cloud",
                {"provider_key": "rytr",
                 "execution_target": "cloud",
                 "prompt": "hello",
                 "est_cost_usd": 0.01, "max_cost_usd": 0.05,
                 "plan_fingerprint": "0" * 64},
                expect=422)
    assert "plan" in r.text


def test_submit_matching_plan_fingerprint_passes_gate(client):
    """A submit pinning the CURRENT plan fingerprint clears the A7
    staleness gate — the approval is proven fresh at commit time,
    then the normal mock submit path runs (simulated env, mock
    adapter, zero egress)."""
    pid = make_project(client)["id"]
    _enable(client, "rytr")
    pv = _preview(client, pid, "cloud.plan",
                  {"provider_key": "rytr", "task": "text",
                   "est_cost_usd": 0.01, "max_cost_usd": 0.05})
    fp = pv["resolved_cloud"]["plan_fingerprint"]
    r = _commit(client, pid, "generate.text.cloud",
                {"provider_key": "rytr",
                 "execution_target": "cloud",
                 "prompt": "hello",
                 "est_cost_usd": 0.01, "max_cost_usd": 0.05,
                 "plan_fingerprint": fp})
    assert r["status"] == "committed" and r["job_id"]

    # A DIFFERENT estimate under the same pin refuses — the approval
    # was for the plan as previewed, not for drifted inputs.
    r2 = _commit(client, pid, "generate.text.cloud",
                 {"provider_key": "rytr",
                  "execution_target": "cloud",
                  "prompt": "hello",
                  "est_cost_usd": 0.02, "max_cost_usd": 0.05,
                  "plan_fingerprint": fp},
                 expect=422)
    assert "plan" in r2.text


def test_sim_submit_records_no_live_activity(client):
    """Everything booked stays in the simulated environment — the plan
    never fabricates live capability, and the audit carries no secret
    material."""
    pid = make_project(client)["id"]
    _enable(client, "sudowrite")
    r = _commit(client, pid, "cloud.plan",
                {"provider_key": "sudowrite", "task": "text"})
    assert r["status"] == "committed"
    from app.db.models import AuditEvent
    import sqlalchemy as sa
    with client.app.state.session_factory() as s:
        ev = s.execute(
            sa.select(AuditEvent)
            .where(sa.cast(AuditEvent.payload_json, sa.String)
                   .like("%cloud.plan%"))
            .order_by(AuditEvent.id.desc())).scalars().first()
        assert ev is not None
        blob = str(ev.payload_json)
        assert "api_key" not in blob and "secret" not in blob.lower()
