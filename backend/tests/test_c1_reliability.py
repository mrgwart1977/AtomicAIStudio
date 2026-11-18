"""C1 — reliability baseline: deterministic fault-injection coverage.

Existing suites already verify: restart reconcile requeue
(test_jobs), idempotent enqueue (test_jobs), queued-cancel spend
release (test_a7_scheduling), adapter cancel races + ffmpeg kill
(test_a4/a5), bounded process exit with stalled scans (C15 probe),
corrupt/partial package rejection (test_m11), missing-blob honesty
(test_m1), migration backfill on genuine pre-release DBs
(test_n1a_tracks). This file adds the gaps: mid-execution adapter
crash, worker-dead attempt state, crash finalize, CAS-registration
rollback, terminal cancel no-op, queued-jobs survive reconcile, and
migration-chain integrity/idempotency/downgrade refusal.

Engine drive is synchronous (``eng._execute`` via asyncio.run) — the
product property under test is failure semantics, never scheduling
latency, so no polling deadlines are involved.
"""
import asyncio

import pytest

from tests.conftest import make_project
from tests.test_c4b_shot_generate import _commit
from tests.test_a2_storyboard import _scene2, _shot2
from tests.test_a3_keyframes import _mk_kf


def _mk_job(client, pid):
    """One real mock gen job row via the command path."""
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"], prompt="s")
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="s")
    out = _commit(client, pid, "keyframe.generate",
                  {"keyframe_id": kf["id"]})
    return out["job_id"]


def _engine(client):
    eng = client.app.state.job_engine

    async def _noop(_job):
        return None

    eng._publish = _noop          # app-loop-bound; stub for asyncio.run
    return eng


def _job(client, pid, job_id):
    return next(j for j in client.get(f"/api/projects/{pid}/jobs").json()
                if j["id"] == job_id)


def _attempts(client, pid, job_id):
    return (_job(client, pid, job_id).get("progress_json") or {}) \
        .get("attempts") or []


# ---------- durable job recovery (C1.3) ----------

def test_adapter_crash_fails_job_and_cleans_up(client):
    """Adapter raising mid-run → job 'failed' with the real error, the
    attempt row closes 'failed', the cancel event is released, and the
    job's work dir is purged — nothing strands in 'running'."""
    pid = make_project(client)["id"]
    job_id = _mk_job(client, pid)
    eng = _engine(client)
    adapter = eng._registry.get(
        _job(client, pid, job_id)["adapter_id"])

    async def boom(_handle):
        yield {"progress": 0.4, "message": "halfway"}
        raise RuntimeError("adapter exploded mid-run")

    adapter.run = boom
    wdir = client.app.state.settings.data_dir / "jobs" / pid
    asyncio.run(eng._execute(job_id))
    job = _job(client, pid, job_id)
    assert job["status"] == "failed"
    assert "adapter exploded" in (job["error"] or "")
    assert job_id not in eng._cancel_events
    assert _attempts(client, pid, job_id)[-1]["outcome"] == "failed"
    assert not any(wdir.glob(f"{job_id}*"))


def test_worker_dead_attempt_state(client):
    """WorkerDeadError is distinguished from a generic adapter error —
    the attempt closes 'worker_dead' so restart/retry tooling can tell
    supervisor crashes from content failures."""
    from app.domain.workers.supervisor import WorkerDeadError
    pid = make_project(client)["id"]
    job_id = _mk_job(client, pid)
    eng = _engine(client)
    adapter = eng._registry.get(
        _job(client, pid, job_id)["adapter_id"])

    async def dead(_handle):
        raise WorkerDeadError("worker process died")
        yield  # pragma: no cover — keeps it an async generator

    adapter.run = dead
    asyncio.run(eng._execute(job_id))
    job = _job(client, pid, job_id)
    assert job["status"] == "failed"
    assert _attempts(client, pid, job_id)[-1]["outcome"] == "worker_dead"


def test_finalize_crash_marks_nonterminal_failed(client, settings):
    """A job left non-terminal by an engine crash is honestly failed —
    never left 'running' until something notices."""
    from app.db.models import GenerationJob
    pid = make_project(client)["id"]
    eng = _engine(client)
    with client.app.state.session_factory() as s:
        s.add(GenerationJob(
            id="jobC1CRASH", project_id=pid, adapter_id="mock.image",
            kind="image", status="running", resumable=True,
            idempotency_key="c1-crash", request_json={}))
        s.commit()
    eng._finalize_crash("jobC1CRASH")
    with client.app.state.session_factory() as s:
        job = s.get(GenerationJob, "jobC1CRASH")
        assert job.status == "failed" and job.error == "engine crash"


def test_asset_registration_failure_rolls_back(client):
    """CAS/asset registration IS job success — a failure rolls back the
    asset rows, honestly fails the job, and purges work files. No
    'succeeded' job without its artifact, no orphan asset rows."""
    pid = make_project(client)["id"]
    job_id = _mk_job(client, pid)
    before = client.get(f"/api/projects/{pid}/assets") \
        .json()["items"]
    eng = _engine(client)

    def explode(_s, _job, _out):
        raise RuntimeError("CAS write failed")

    eng._register_asset = explode
    asyncio.run(eng._execute(job_id))
    job = _job(client, pid, job_id)
    assert job["status"] == "failed"
    assert "asset registration failed" in (job["error"] or "")
    after = client.get(f"/api/projects/{pid}/assets").json()["items"]
    assert {a["id"] for a in after} == {a["id"] for a in before}


def test_cancel_request_on_terminal_job_is_noop(client):
    """request_cancel on an already-terminal job returns 'terminal'
    and mutates nothing — stale cancel clicks can't corrupt state."""
    pid = make_project(client)["id"]
    job_id = _mk_job(client, pid)
    eng = _engine(client)
    asyncio.run(eng._execute(job_id))
    job = _job(client, pid, job_id)
    assert job["status"] in {"succeeded", "failed"}
    hint = eng.request_cancel(job_id)
    assert hint == "terminal"
    assert _job(client, pid, job_id)["status"] == job["status"]
    assert _job(client, pid, job_id)["cancel_requested"] in (False, None)


def test_queued_jobs_untouched_by_reconcile(client, settings):
    """Startup reconcile requeues interrupted *running* jobs only —
    genuinely queued rows keep their place and payload verbatim."""
    from app.db.models import GenerationJob
    pid = make_project(client)["id"]
    eng = _engine(client)
    with client.app.state.session_factory() as s:
        s.add(GenerationJob(
            id="jobC1QUE", project_id=pid, adapter_id="mock.image",
            kind="image", status="queued", resumable=True,
            idempotency_key="c1-q", request_json={"prompt": "keep"}))
        s.commit()
    with client.app.state.session_factory() as s:
        n = eng.reconcile_interrupted(s)
        s.commit()
    assert n == 0
    with client.app.state.session_factory() as s:
        job = s.get(GenerationJob, "jobC1QUE")
        assert job.status == "queued"
        assert job.request_json["prompt"] == "keep"


# ---------- migration safety (C1.5) ----------

def test_migration_chain_is_linear_and_complete():
    """Every down_revision resolves, exactly one head exists, and the
    chain from head reaches base with no cycles — a broken link would
    strand installs mid-upgrade."""
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    from app.main import _alembic_script_location
    cfg = AlembicConfig()
    cfg.set_main_option("script_location",
                      str(_alembic_script_location()))
    script = ScriptDirectory.from_config(cfg)
    revs = {r.revision: r for r in script.walk_revisions()}
    heads = script.get_heads()
    assert len(heads) == 1, f"expected single head, got {heads}"
    seen, node = set(), heads[0]
    while node is not None:
        assert node not in seen, f"migration cycle at {node}"
        seen.add(node)
        down = revs[node].down_revision
        if isinstance(down, tuple):
            assert all(d in revs for d in down), \
                f"{node} down_revision missing: {down}"
            node = down[0]
        else:
            assert down is None or down in revs, \
                f"{node} down_revision missing: {down}"
            node = down
    assert seen == set(revs), \
        f"unreachable revisions: {set(revs) - seen}"


def test_run_migrations_idempotent(settings):
    """Upgrading an already-current database is a no-op, not an error —
    repeated launches and reinstalls are safe."""
    from app.main import run_migrations
    run_migrations(settings.db_url)
    run_migrations(settings.db_url)   # must not raise
    from app.db.session import create_engine_for
    eng = create_engine_for(settings.db_url)
    with eng.connect() as con:
        ver = con.exec_driver_sql(
            "SELECT version_num FROM alembic_version").scalar()
    assert ver is not None


def test_newer_stamped_db_refuses_upgrade(settings):
    """A database stamped by a NEWER build must fail closed — silent
    downgrade would corrupt rows the old schema can't express."""
    import pytest as _pt
    from app.main import run_migrations, UnsupportedRevisionError
    from app.db.session import create_engine_for
    run_migrations(settings.db_url)
    eng = create_engine_for(settings.db_url)
    with eng.connect() as con:
        con.exec_driver_sql(
            "UPDATE alembic_version SET version_num='zz99_future'")
        con.commit()
    with _pt.raises(UnsupportedRevisionError):
        run_migrations(settings.db_url)
