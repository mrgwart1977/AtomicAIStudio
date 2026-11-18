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


class _StubVault:
    """Credential PRESENCE double — status() reports configured but
    has no secret material to return (same shape as test_c2 stubs)."""
    def status(self, key):
        return {"configured": True, "fields": ["api_key"],
                "set_at": "2026-01-01T00:00:00Z",
                "readable": True, "unreadable_reason": None}


def test_submit_matching_plan_fingerprint_passes_gate(client):
    """A submit pinning the CURRENT plan fingerprint clears the A7
    staleness gate — the approval is proven fresh at commit time,
    then the normal mock submit path runs (simulated env, mock
    adapter, zero egress)."""
    pid = make_project(client)["id"]
    _enable(client, "rytr")
    client.app.state.provider_vault = _StubVault()
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


# ==================================================================
# A7 final hardening (F2–F7)
# ==================================================================

# ------------------------------------------------ F2: admission atomicity

def test_concurrent_admission_is_atomic_no_oversubscribe():
    """Threads racing the same device: check-then-claim is atomic —
    winners fit exactly inside usable VRAM, losers refuse, ownership
    is per-job, and the accounting can never exceed the device."""
    import threading
    s = _sched(_hw([8]))
    admitted, refused = [], []

    def attempt(i):
        r = s.acquire(f"j{i}", {}, est_vram=2 * GIB)
        (admitted if r == 0 else refused).append(i)

    threads = [threading.Thread(target=attempt, args=(i,))
               for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 4            # 8 GiB / 2 GiB exactly
    assert len(refused) == 12
    jobs = s.slots()[0]["jobs"]
    assert len(jobs) == 4
    assert sum(jobs.values()) <= 8 * GIB
    assert all(v == 2 * GIB for v in jobs.values())
    # every admitted job owns exactly its claim
    for i in admitted:
        assert s.claimed_bytes(f"j{i}") == 2 * GIB


def test_concurrent_exclusive_claims_single_winner():
    """N simultaneous unknown-footprint claims on one device: exactly
    one wins — exclusivity is atomic, never 'both thought it was free'."""
    import threading
    s = _sched(_hw([8]))
    winners = []

    def attempt(i):
        if s.acquire(f"x{i}", {}, est_vram=None) == 0:
            winners.append(i)

    threads = [threading.Thread(target=attempt, args=(i,))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1
    # The winner reserved the device's full usable bytes.
    assert s.claimed_bytes(f"x{winners[0]}") == 8 * GIB


def test_per_job_refusal_reasons_survive_concurrency():
    """Queue reasons are owned per job — a later refusal never
    rewrites why an earlier job is still waiting."""
    s = _sched(_hw([4]))
    s.acquire("holder", {}, est_vram=4 * GIB)
    assert s.acquire("too-big", {}, est_vram=8 * GIB) is None
    assert s.acquire("missing", {"device": 9}) is None
    assert "fits" in s.reason_for("too-big")
    assert "not present" in s.reason_for("missing")
    # Another refusal does not clobber earlier jobs' reasons.
    assert s.acquire("other", {}, est_vram=8 * GIB) is None
    assert "not present" in s.reason_for("missing")
    # A successful claim clears that job's stale reason.
    s.release("holder")
    assert s.acquire("too-big", {}, est_vram=4 * GIB) == 0
    assert s.reason_for("too-big") is None


def test_headroom_usable_boundary_exact():
    """usable = vram - headroom; a request at exactly usable admits,
    one byte over refuses — headroom is never silently borrowed."""
    s = _sched(_hw([8]), prefs={"vram_headroom_bytes": 2 * GIB})
    assert s.slots()[0]["usable_bytes"] == 6 * GIB
    assert s.acquire("exact", {}, est_vram=6 * GIB) == 0
    assert s.acquire("plus-one", {}, est_vram=1) is None
    assert "free" in s.last_reason or "usable" in s.last_reason


def test_explicit_device_overrides_preference():
    """Request device beats the preferred_gpu default; the preference
    only fills in when the request names no device."""
    s = _sched(_hw([4, 8]), prefs={"preferred_gpu": 1})
    assert s.acquire("pinned", {"device": 0}, est_vram=2 * GIB) == 0
    assert s.acquire("defaulted", {}, est_vram=2 * GIB) == 1


# ------------------------------------------------- F3: lifecycle

def test_release_idempotent_and_unknown_safe():
    """Duplicate completion callbacks and releases for jobs that never
    held a reservation are both harmless no-ops."""
    s = _sched(_hw([4]))
    s.release("never-held")
    s.acquire("j1", {}, est_vram=2 * GIB)
    s.release("j1")
    s.release("j1")
    assert s.reservations() == {}
    assert s.claimed_bytes("j1") is None
    assert s.reason_for("j1") is None


def test_exclusive_claim_records_actual_reserved_bytes(client):
    """reserved_vram_bytes persists the ACTUAL claim — an exclusive
    (unknown-footprint) job reserves the device's usable bytes, not
    the request's missing estimate."""
    _use_sim_gpus(client, [8])
    pid = make_project(client)["id"]
    r = _sim_job(client, pid, {"work_ms": 8000})  # no est → exclusive
    jid = r["job_id"]
    import time
    for _ in range(100):
        j = _job(client, pid, jid)
        if j["status"] == "running":
            break
        time.sleep(0.05)
    sched = client.app.state.job_engine._sched
    assert sched.claimed_bytes(jid) == 8 * GIB
    j = _job(client, pid, jid)
    assert j["reserved_vram_bytes"] == 8 * GIB
    client.post(f"/api/jobs/{jid}/cancel")
    wait_for_job(client, pid, jid, timeout=30)


def test_scheduler_state_reports_simulated_flag(client):
    _use_sim_gpus(client, [8])
    make_project(client)
    st = client.get("/api/gpu/scheduler").json()
    # dict probes aren't marked simulated — the field exists and is
    # honest either way (real sim inventories come from ATOMIC_HW_JSON).
    assert "simulated" in st and st["simulated"] is False


# ------------------------------------------------- F4: multi-GPU behavior

def test_device_return_admits_again():
    """A vanished device whose job ended rejoins cleanly when the probe
    reports it again — no stale accounting, no permanent loss."""
    hw = _hw([8, 8])
    s = _sched(hw)
    assert s.acquire("j1", {"device": 1}, est_vram=4 * GIB) == 1
    s._probe = _hw([8])           # device 1 disappears
    assert s.lost_devices() == [1]
    s.release("j1")               # the job's execution ended
    s._probe = hw                 # device returns
    assert s.lost_devices() == []
    assert s.acquire("j2", {"device": 1}, est_vram=4 * GIB) == 1
    assert s.slots()[1]["jobs"] == {"j2": 4 * GIB}


def test_vram_never_pooled_across_devices():
    """16 GiB spread over two 8 GiB devices still cannot host a 10 GiB
    job — admission is per-device, always."""
    s = _sched(_hw([8, 8]))
    assert s.acquire("huge", {}, est_vram=10 * GIB) is None
    assert "no device fits" in s.last_reason


def test_heterogeneous_backend_requirements():
    """cuda → NVIDIA-only; directml → vendor-neutral; vulkan → ICD +
    any GPU. Ties deterministically prefer the lower index."""
    hw = _hw([8, 8])
    hw["gpus"][1]["vendor"] = "AMD"
    s = _sched(hw)
    assert s.acquire("c1", {}, est_vram=2 * GIB,
                     requires=["cuda"]) == 0          # NVIDIA only
    assert s.acquire("d1", {}, est_vram=2 * GIB,
                     requires=["directml"]) == 1       # most free: 8 vs 6
    # 6 GiB free on each → stable order picks device 0.
    assert s.acquire("v1", {}, est_vram=2 * GIB,
                     requires=["vulkan"]) == 0


def test_fallback_cpu_when_no_gpu_and_allowed():
    s = _sched(_hw([]))
    assert s.acquire("j1", {"allow_cpu": True}) == -1
    assert s.acquire("j2", {"allow_cpu": False}) is None
    assert "no GPU" in s.last_reason


# ------------------------------------------------- F5: sim isolation

def test_simulated_inventory_marked_and_no_bypass(monkeypatch):
    """ATOMIC_HW_JSON reports are always flagged simulated; simulated
    devices pass through the SAME compat + admission gates — the hook
    changes the inventory, never the rules."""
    import json as _json
    hw = _hw([4])
    hw["gpus"][0]["vendor"] = "AMD"
    monkeypatch.setenv("ATOMIC_HW_JSON", _json.dumps(hw))
    from app.domain import hardware
    rep = hardware.probe(force=True)
    assert rep.get("simulated") is True
    s = _sched(lambda: rep)
    assert s.state()["simulated"] is True
    # Vendor mismatch still refuses on the simulated inventory.
    assert s.acquire("c", {}, requires=["cuda"]) is None
    # Admission limits still bind.
    assert s.acquire("big", {}, est_vram=9 * GIB,
                     requires=["vulkan"]) is None
    # Unset → real probe, unmarked.
    monkeypatch.delenv("ATOMIC_HW_JSON")
    assert not hardware.probe(force=True).get("simulated")


def test_malformed_hw_json_falls_back_to_real_probe(monkeypatch):
    """Unreadable/invalid ATOMIC_HW_JSON never wedges the probe — it
    falls back to real hardware rather than reporting phantom devices."""
    from app.domain import hardware
    monkeypatch.setenv("ATOMIC_HW_JSON", "{not json")
    assert not hardware.probe(force=True).get("simulated")
    monkeypatch.setenv("ATOMIC_HW_JSON", "/nonexistent/sim-hw.json")
    assert not hardware.probe(force=True).get("simulated")
    monkeypatch.setenv("ATOMIC_HW_JSON", '["not", "an", "object"]')
    assert not hardware.probe(force=True).get("simulated")


def test_sim_adapter_is_in_process_and_mock_kind():
    """mock.gpu.sim declares its truth: adapter_kind=mock, needs_gpu —
    it exercises the scheduler, it is never inference."""
    from app.adapters.gpu_sim import GpuSimAdapter
    a = GpuSimAdapter()
    assert a.profile.adapter_id == "mock.gpu.sim"
    assert a.profile.adapter_kind == "mock"
    assert a.profile.needs_gpu is True


# ------------------------------------------------- F6: cloud authz

def test_queued_cancel_releases_spend_reservation(client):
    """A job cancelled before dispatch returns its spend hold — the
    reservation must not strand 'open' forever on a queued cancel."""
    _use_sim_gpus(client, [4])
    pid = make_project(client)["id"]
    _enable(client, "rytr")
    # Park the gen lane so the cloud job stays queued.
    park = _sim_job(client, pid, {"est_vram_bytes": 2 * GIB,
                                  "work_ms": 30000})
    import time
    for _ in range(100):
        if _job(client, pid, park["job_id"])["status"] == "running":
            break
        time.sleep(0.05)
    r = _commit(client, pid, "generate.text.cloud",
                {"provider_key": "rytr", "execution_target": "cloud",
                 "prompt": "held", "est_cost_usd": 0.01,
                 "max_cost_usd": 0.05})
    jid = r["job_id"]
    for _ in range(100):
        if _job(client, pid, jid)["status"] == "queued":
            break
        time.sleep(0.05)
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 200
    assert _job(client, pid, jid)["status"] == "cancelled"
    from app.db.models import SpendReservation
    import sqlalchemy as sa
    with client.app.state.session_factory() as s:
        res = s.execute(sa.select(SpendReservation).where(
            SpendReservation.job_id == jid)).scalar_one()
        assert res.status == "released"
    client.post(f"/api/jobs/{park['job_id']}/cancel")


def test_pinned_submit_refuses_nonadmitting_plan(client):
    """Pinning a fingerprint reproduces the plan — a plan that would
    NOT admit (missing credential) refuses even when the fingerprint
    matches. Approval of a non-admitting plan is not authorization."""
    pid = make_project(client)["id"]
    _enable(client, "rytr")   # enabled but NO credential configured
    pv = _preview(client, pid, "cloud.plan",
                  {"provider_key": "rytr", "task": "text",
                   "est_cost_usd": 0.01, "max_cost_usd": 0.05})
    rc = pv["resolved_cloud"]
    assert rc["would_admit"] is False
    fp = rc["plan_fingerprint"]
    assert fp
    r = _commit(client, pid, "generate.text.cloud",
                {"provider_key": "rytr", "execution_target": "cloud",
                 "prompt": "hello", "est_cost_usd": 0.01,
                 "max_cost_usd": 0.05, "plan_fingerprint": fp},
                expect=422)
    assert rc["availability"] in r.text or "plan" in r.text


def test_plan_is_read_only_and_touches_no_network(client, monkeypatch):
    """resolved_plan writes nothing and opens no sockets — planning is
    a pure read of catalog + vault presence + budget state."""
    pid = make_project(client)["id"]
    _enable(client, "rytr")
    import socket
    def _no_net(*a, **k):
        raise AssertionError("network access during cloud.plan")
    monkeypatch.setattr(socket.socket, "connect", _no_net)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_net)
    from app.domain import cloudplan as _cp
    from app.db.models import Project
    with client.app.state.session_factory() as s:
        project = s.get(Project, pid)
        res = _cp.resolved_plan(
            s, project, provider_key="rytr", task="text",
            vault=client.app.state.provider_vault,
            registry=client.app.state.adapter_registry)
        assert res["plan_fingerprint"]
        assert not s.new and not s.dirty
