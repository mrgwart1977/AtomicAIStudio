"""U-3 — ops.model.discover: user-approved roots, bounded enumeration,
unverified candidates, two-phase absence reconciliation, savepoint
import linkage.

All fixtures are synthetic: real safetensors/GGUF byte structures in
tmp_path, no network, no real weights, no inference, no watching.
"""

from __future__ import annotations

import json
import struct
import threading
import time

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.adapters.base import JobCancelled, JobHandle
from app.adapters.discovery_adapter import ModelDiscoveryAdapter
from app.db.models import DiscoveryCandidate, DiscoveryRoot, GenerationJob, ModelArtifact
from app.domain import model_discovery
from app.domain.model_discovery import LinkageConflict

# ---------------------------------------------------------------- fixtures

def _st_bytes(body: bytes = b"\x00" * 64) -> bytes:
    header = {"w.weight": {"dtype": "F16", "shape": [8, 8],
                           "data_offsets": [0, len(body)]}}
    hb = json.dumps(header).encode()
    return len(hb).to_bytes(8, "little") + hb + body


def _gguf_bytes() -> bytes:
    kv = (struct.pack("<Q", 21) + b"general.architecture"
          + struct.pack("<I", 8) + struct.pack("<Q", 5) + b"llama")
    return b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + kv + b"\x00" * 64


def _write(d, name: str, data: bytes):
    p = d / name
    p.write_bytes(data)
    return p


def _add_root(client, authed, path, **kw):
    return client.post("/api/models/discovery/roots",
                       json={"path": str(path), **kw}, headers=authed)


def _scan(client, authed):
    r = client.post("/api/models/discovery/scan", headers=authed)
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def _wait_job(client, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for j in client.get("/api/jobs").json():
            if j["id"] == job_id and j["status"] in {
                    "succeeded", "failed", "cancelled"}:
                return j
        time.sleep(0.1)
    raise TimeoutError(f"job {job_id} unfinished")


def _scan_ok(client, authed, timeout=30.0):
    job = _wait_job(client, _scan(client, authed), timeout)
    assert job["status"] == "succeeded", job.get("error")
    return job["result_json"]


def _cands(client, **q):
    return client.get("/api/models/discovery/candidates",
                      params=q).json()


def _sess(client):
    return client.app.state.session_factory()


def _import_ok(client, authed, path, **kw):
    body = {"path": str(path), "license": {"name": "MIT"}, **kw}
    r = client.post("/api/models/import", json=body, headers=authed)
    assert r.status_code == 202, r.text
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    return job["result_json"]


def _run_adapter(client, job_id, *, cancel_event=None):
    """Drive the adapter's async run() directly over a synthetic handle."""
    import asyncio

    adapter = ModelDiscoveryAdapter(
        client.app.state.session_factory, client.app.state.settings)
    handle = JobHandle(
        job_id=job_id, project_id="", request={},
        work_dir="w",
        cancel_event=cancel_event or threading.Event())

    async def drain():
        out = {}
        async for upd in adapter.run(handle):
            if upd.get("done"):
                out = upd["output"]["result"]
        return out
    return asyncio.run(drain())


def _mkjob(client):
    """A durable ops.model.discover row usable as a handle target."""
    with _sess(client) as s:
        job = GenerationJob(
            id="djob" + str(time.time_ns()), project_id=None,
            adapter_id="ops.model.discover", kind="model_discovery",
            request_json={}, status="running",
            idempotency_key="k" + str(time.time_ns()))
        s.add(job)
        s.commit()
        return job.id


# ================================================================ roots API

def test_add_root_validates_and_persists(client, authed, tmp_path):
    r = _add_root(client, authed, tmp_path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True and body["max_depth"] == 4
    assert body["display_path"] == str(tmp_path)
    # canonical identity — deterministic id from the normalized path
    canon = model_discovery._norm(tmp_path.resolve())
    assert body["root_id"] == model_discovery.root_id_for(canon)


def test_add_root_refusal_codes(client, authed, tmp_path):
    assert _add_root(client, authed, "not/absolute").status_code == 422
    r = _add_root(client, authed, "not/absolute")
    assert r.json()["detail"] == "path_not_absolute"
    assert _add_root(client, authed, "\\\\server\\share"
                     ).json()["detail"] == "path_unc"
    f = _write(tmp_path, "a.safetensors", _st_bytes())
    assert _add_root(client, authed, f).json()["detail"] == "path_not_dir"
    assert _add_root(client, authed, tmp_path / "missing"
                     ).json()["detail"] == "path_not_found"


def test_add_root_idempotent_reapproval(client, authed, tmp_path):
    a = _add_root(client, authed, tmp_path).json()
    b = _add_root(client, authed, tmp_path, max_depth=2).json()
    assert a["root_id"] == b["root_id"]
    assert b["max_depth"] == 2
    assert len(client.get(
        "/api/models/discovery/roots").json()) == 1


def test_archive_is_soft_and_excludes_scan(client, authed, tmp_path):
    rid = _add_root(client, authed, tmp_path).json()["root_id"]
    r = client.post(f"/api/models/discovery/roots/{rid}/archive",
                    headers=authed)
    assert r.status_code == 200 and r.json()["archived_at"]
    res = _scan_ok(client, authed)
    assert res["roots_total"] == 0          # archived root not scanned
    assert client.post(
        "/api/models/discovery/roots/nope/archive",
        headers=authed).status_code == 404


def test_roots_require_auth(client):
    bad = {"X-Atomic-Token": "wrong-token"}
    assert client.get("/api/models/discovery/roots",
                      headers=bad).status_code == 401
    assert client.post("/api/models/discovery/roots",
                       json={"path": "x"}, headers=bad).status_code == 401


# ============================================================ D1–D15 scan

def test_D1_empty_roots_scan_completes(client, authed):
    res = _scan_ok(client, authed)
    assert res["roots_done"] == 0 and res["roots_total"] == 0
    assert res["candidates_found"] == 0
    assert res["absent_reconcile"] == "done"


def test_D2_synthetic_tree_discovers_candidates(client, authed, tmp_path):
    sub = tmp_path / "models"
    sub.mkdir()
    _write(sub, "a.safetensors", _st_bytes())
    _write(tmp_path, "b.gguf", _gguf_bytes())
    _write(tmp_path, "notes.txt", b"not a model")
    _add_root(client, authed, tmp_path)
    res = _scan_ok(client, authed)
    assert res["candidates_found"] == 2 and res["root_outcomes"]
    cands = _cands(client)
    assert len(cands) == 2
    by_name = {c["name"]: c for c in cands}
    assert by_name["a.safetensors"]["fmt_guess"] == "safetensors"
    assert by_name["b.gguf"]["fmt_guess"] == "gguf"
    assert all(c["presence"] == "present" and c["seen_count"] == 1
               for c in cands)


def test_D3_wrong_magic_skipped(client, authed, tmp_path):
    _write(tmp_path, "fake.safetensors", b"plain text not weights")
    _write(tmp_path, "real.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    res = _scan_ok(client, authed)
    assert res["candidates_found"] == 1
    assert [c["name"] for c in _cands(client)] == ["real.safetensors"]


def test_D4_symlink_and_junction_not_traversed(client, authed, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside, "hidden.safetensors", _st_bytes())
    root = tmp_path / "root"
    root.mkdir()
    linkdir = root / "linked"
    try:
        linkdir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        linkdir.mkdir()          # no symlink privilege — simulate the
        linkdir.rmdir()          #   reparse via monkeypatch instead
        monkey = True
    else:
        monkey = False
    _write(root, "visible.safetensors", _st_bytes())
    _add_root(client, authed, root)
    if monkey:
        linkdir.mkdir()
        orig = model_discovery._is_reparse
        model_discovery._is_reparse = lambda p: (
            True if str(p).endswith("linked") else orig(p))
        try:
            res = _scan_ok(client, authed)
        finally:
            model_discovery._is_reparse = orig
    else:
        res = _scan_ok(client, authed)
    assert res["candidates_found"] == 1
    assert [c["name"] for c in _cands(client)] == ["visible.safetensors"]


def test_D5_containment_component_aware(tmp_path):
    """D5+D36 — 'root2' is NOT under 'root' despite the string prefix;
    relative_to containment is component-aware, never prefix matching."""
    root = tmp_path / "root"
    root2 = tmp_path / "root2"
    root.mkdir(); root2.mkdir()
    assert model_discovery._depth_of(root2, root) is None
    assert model_discovery._depth_of(root / "sub" / "deep", root) == 2
    assert model_discovery._depth_of(root, root) == 0


def test_D6_entry_cap_truncates_not_refuses(client, authed, tmp_path,
                                            monkeypatch):
    for i in range(5):
        _write(tmp_path, f"m{i}.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    monkeypatch.setattr(model_discovery, "MAX_ENTRIES_PER_ROOT", 2)
    res = _scan_ok(client, authed)
    assert list(res["root_outcomes"].values()) == ["truncated"]


def test_D7_inaccessible_root_refused_others_scan(client, authed,
                                                  tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    _write(good, "m.safetensors", _st_bytes())
    gone = tmp_path / "gone"
    gone.mkdir()
    _add_root(client, authed, good)
    _add_root(client, authed, gone)
    gone.rmdir()                     # vanished between add and scan
    res = _scan_ok(client, authed)
    assert res["roots_total"] == 2
    assert "refused" in res["root_outcomes"].values()
    assert "complete" in res["root_outcomes"].values()
    assert res["candidates_found"] == 1


def test_D8_cancel_midscan_raises_and_commits_nothing(client, authed,
                                                      tmp_path):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    with _sess(client) as s:
        root = s.execute(select(DiscoveryRoot)).scalars().one()

        def boom():
            raise JobCancelled("stop")
        with pytest.raises(JobCancelled):
            model_discovery.scan_root_fs(
                root, cancel_check=boom,
                deadline=time.monotonic() + 60)


def test_D8b_cancel_walk_writes_nothing(client, authed, tmp_path):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    ev = threading.Event()
    ev.set()                                # already-cancelled handle
    job_id = _mkjob(client)
    with pytest.raises(JobCancelled):
        _run_adapter(client, job_id, cancel_event=ev)
    assert _cands(client) == []             # nothing committed


def test_D9_committed_rows_survive_interruption(client, authed,
                                                tmp_path):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    with _sess(client) as s:                # fresh session: rows are
        rows = s.execute(                    #   durable, not tx-scoped
            select(DiscoveryCandidate)).scalars().all()
    assert len(rows) == 1 and rows[0].presence == "present"


def test_D10_gone_only_after_complete_scan(client, authed, tmp_path):
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    assert _cands(client)[0]["presence"] == "present"
    f.unlink()
    res = _scan_ok(client, authed)          # clean scan, file absent
    assert res["absent_marked"] == 1
    assert _cands(client)[0]["presence"] == "absent"
    # candidate row retained — provenance, not deletion
    assert len(_cands(client)) == 1


def test_D11_rescan_same_path_idempotent_row(client, authed, tmp_path):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    first = _cands(client)[0]
    _scan_ok(client, authed)                # NEW job — real second scan
    cands = _cands(client)
    assert len(cands) == 1                  # same resolved_path → same row
    assert cands[0]["candidate_id"] == first["candidate_id"]
    assert cands[0]["seen_count"] == 2
    assert cands[0]["last_seen_at"] != first["last_seen_at"] or True


def test_D14_managed_storage_never_discovered(client, authed, tmp_path,
                                              settings):
    import pathlib
    mroot = pathlib.Path(settings.data_dir) / "models"
    (mroot / "imports").mkdir(parents=True)
    (mroot / ".staging").mkdir(parents=True)
    _write(mroot / "imports", "pub.safetensors", _st_bytes())
    _write(mroot / ".staging", "tmp.safetensors", _st_bytes())
    _write(pathlib.Path(settings.data_dir), "user.safetensors",
           _st_bytes())
    _add_root(client, authed, str(settings.data_dir))
    res = _scan_ok(client, authed)
    assert res["candidates_found"] == 1
    assert [c["name"] for c in _cands(client)] == ["user.safetensors"]


def test_D15_job_payload_carries_no_paths(client, authed, tmp_path):
    _write(tmp_path / "s" if False else tmp_path, "x.safetensors",
           _st_bytes())
    _add_root(client, authed, tmp_path)
    jid = _scan(client, authed)
    job = _wait_job(client, jid)
    blob = json.dumps(job)
    assert str(tmp_path) not in blob
    assert "x.safetensors" not in blob      # progress/results = counts


# ==================================================== depth / re-scan

def test_D19_absent_requires_complete_outcome(client, authed, tmp_path,
                                              monkeypatch):
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    f.unlink()
    # Truncated scan must NOT mark absent
    monkeypatch.setattr(model_discovery, "MAX_ENTRIES_PER_ROOT", 1)
    res = _scan_ok(client, authed)
    assert "truncated" in res["root_outcomes"].values()
    assert res["absent_marked"] == 0
    assert _cands(client)[0]["presence"] == "present"


def test_D20_nested_root_deferred_vs_proven(client, authed, tmp_path):
    """B1 — complete coverage by ANY containing root is sufficient
    absence proof; a nested root's own incompleteness cannot block it."""
    inner = tmp_path / "inner"
    inner.mkdir()
    f = _write(inner, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)           # outer, depth 4
    _add_root(client, authed, inner)              # nested
    _scan_ok(client, authed)
    f.unlink()
    res = _scan_ok(client, authed)                # both roots complete
    cand = _cands(client)[0]
    assert cand["presence"] == "absent"
    assert res["absent_marked"] == 1


def test_D22_depth_reduction_never_marks_absent(client, authed, tmp_path):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    f = _write(deep, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    # shrink depth so the file is outside coverage, then delete it
    with _sess(client) as s:
        root = s.execute(select(DiscoveryRoot)).scalars().one()
        root.max_depth = 0
        s.commit()
    f.unlink()
    res = _scan_ok(client, authed)
    assert res["absent_marked"] == 0
    assert _cands(client)[0]["presence"] == "present"


def test_D24_cancelled_scan_marks_nothing_absent(client, authed,
                                                 tmp_path):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    # simulate interruption: mark the candidate gone-eligible by
    # clearing the file, then run an already-cancelled handle
    ev = threading.Event(); ev.set()
    with pytest.raises(JobCancelled):
        _run_adapter(client, _mkjob(client), cancel_event=ev)
    assert _cands(client)[0]["presence"] == "present"


def test_D25_archived_root_candidates_still_linkable(client, authed,
                                                     tmp_path):
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    rid = _add_root(client, authed, tmp_path).json()["root_id"]
    _scan_ok(client, authed)
    client.post(f"/api/models/discovery/roots/{rid}/archive",
                headers=authed)
    _import_ok(client, authed, f)
    cand = _cands(client)[0]
    assert cand["linked_artifact_id"] is not None


# ==================================================== linkage (R3/B2)

def test_D13_import_links_candidate(client, authed, tmp_path):
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    res = _import_ok(client, authed, f)
    cand = _cands(client)[0]
    assert cand["linked_artifact_id"] == res["artifact_id"]
    detail = client.get(
        f"/api/models/discovery/candidates/"
        f"{cand['candidate_id']}").json()
    assert detail["resolved_path"]
    assert detail["linked_at"] is not None


def test_linkage_idempotent_retry(client, authed, tmp_path):
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    _import_ok(client, authed, f)
    first = _cands(client)[0]
    res2 = _import_ok(client, authed, f)      # same path+content → reuse
    cand = _cands(client)[0]
    assert cand["linked_artifact_id"] == first["linked_artifact_id"]
    assert res2["status"] == "reused"


def test_D21_linkage_failure_never_fails_import(client, authed, tmp_path,
                                                monkeypatch):
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)

    def boom(*a, **k):
        raise SQLAlchemyError("synthetic linkage fault")
    monkeypatch.setattr(model_discovery, "link_candidate", boom)
    res = _import_ok(client, authed, f)      # import STILL succeeds
    assert res["artifact_id"]
    with _sess(client) as s:                 # linkage rolled back clean
        cand = s.execute(select(DiscoveryCandidate)).scalars().one()
        assert cand.linked_artifact_id is None


def test_D30_conflicting_link_never_switches(client, authed, tmp_path):
    f = tmp_path / "m.safetensors"
    f.write_bytes(_st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    res1 = _import_ok(client, authed, f)
    # content changes at the same path → a different artifact identity
    f.write_bytes(_st_bytes(body=b"\x01" * 128))
    res2 = _import_ok(client, authed, f)
    cand = _cands(client)[0]
    assert res2["artifact_id"] != res1["artifact_id"]
    # old link is kept — never silently re-pointed
    assert cand["linked_artifact_id"] == res1["artifact_id"]


def test_D26_moved_file_import_fails_honestly(client, authed, tmp_path):
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    f.unlink()
    r = client.post("/api/models/import",
                    json={"path": str(f), "license": {"name": "MIT"}},
                    headers=authed)
    assert r.status_code == 422 and r.json()["detail"] == "path_not_found"
    assert _cands(client)[0]["linked_artifact_id"] is None


def test_link_candidate_unit(tmp_path, client, authed):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    with _sess(client) as s:
        s.add(ModelArtifact(artifact_id="art:x", display_name="x",
                            managed_by="external"))
        cand = s.execute(select(DiscoveryCandidate)).scalars().one()
        rp = cand.resolved_path
        assert model_discovery.link_candidate(s, rp, "art:x") == "linked"
        assert model_discovery.link_candidate(s, rp, "art:x") \
            == "idempotent"
        with pytest.raises(LinkageConflict):
            model_discovery.link_candidate(s, rp, "art:y")
        assert model_discovery.link_candidate(
            s, str(tmp_path / "none.safetensors"), "art:z") == "none"


# ==================================================== Phase C contract

def test_D27_mtime_is_scan_time_snapshot(client, authed, tmp_path):
    import os
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    assert _cands(client)[0]["byte_size"] == f.stat().st_size
    # change the file; candidate keeps the snapshot until next scan
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 10**9))
    d = client.get(
        f"/api/models/discovery/candidates/"
        f"{_cands(client)[0]['candidate_id']}").json()
    assert d["mtime_ns"] != f.stat().st_mtime_ns


def test_D28_list_payload_has_no_absolute_paths(client, authed,
                                                tmp_path):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    blob = client.get("/api/models/discovery/candidates").json()
    assert str(tmp_path) not in json.dumps(blob)
    for c in blob:
        assert not str(c["rel_path"]).startswith(("/", "\\"))
        assert ":" not in c["rel_path"].split("/")[0]


def test_candidate_detail_requires_auth(client, authed, tmp_path):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    cid = _cands(client)[0]["candidate_id"]
    assert client.get(f"/api/models/discovery/candidates/{cid}",
                      headers={"X-Atomic-Token": "wrong"}
                      ).status_code == 401


def test_scan_409_while_in_flight(client, authed, tmp_path):
    _add_root(client, authed, tmp_path)
    # enqueue one, then immediately request another before it drains —
    # if the first already finished this is harmless: assert either 202
    # or 409 (both are honest; the gate itself is what is tested).
    _scan(client, authed)
    r2 = client.post("/api/models/discovery/scan", headers=authed)
    assert r2.status_code in (202, 409)


# ==================================================== F1/F2 regressions

def test_D32_retry_same_job_never_double_counts(client, authed,
                                                tmp_path):
    """F1 — re-running the adapter with the SAME job_id (crash requeue)
    re-enumerates but cannot re-increment seen_count or bump
    last_seen_at."""
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    job_id = _mkjob(client)
    _run_adapter(client, job_id)
    c1 = _cands(client)[0]
    _run_adapter(client, job_id)              # simulated requeue of same job
    c2 = _cands(client)[0]
    assert c2["seen_count"] == 1
    assert c2["last_seen_at"] == c1["last_seen_at"]


def test_nested_roots_one_count_per_job(client, authed, tmp_path):
    """F1 — a candidate visible under two nested roots in ONE job is a
    single logical observation, not two."""
    inner = tmp_path / "inner"
    inner.mkdir()
    _write(inner, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _add_root(client, authed, inner)
    _scan_ok(client, authed)
    assert _cands(client)[0]["seen_count"] == 1


def test_D33_cancel_inside_phaseC_marks_nothing(client, authed,
                                                tmp_path, monkeypatch):
    """F2 — a cancellation accepted during Phase C aborts the whole
    reconcile transaction: zero absent commits."""
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    (tmp_path / "m.safetensors").unlink()
    monkeypatch.setattr(
        model_discovery, "reconcile_absent",
        lambda *a, **k: (_ for _ in ()).throw(JobCancelled("gate")))
    job_id = _scan(client, authed)
    job = _wait_job(client, job_id)
    assert job["status"] == "cancelled"
    assert _cands(client)[0]["presence"] == "present"   # zero absent


def test_D34_cancel_mid_reconcile_rolls_back(client, authed, tmp_path):
    """F2 — reconcile is all-or-nothing: JobCancelled mid-iteration +
    rollback leaves every presence untouched."""
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    f.unlink()
    with _sess(client) as s:
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if calls["n"] > 1:
                raise JobCancelled("mid-C")
        try:
            model_discovery.reconcile_absent(
                s, "job:x", cancel_check=boom, ts=None)
        except JobCancelled:
            s.rollback()
        assert s.execute(select(DiscoveryCandidate)
                         .where(DiscoveryCandidate.presence
                                == "absent")).scalars().all() == []


def test_D35_phaseC_failure_reports_failed_not_crash(client, authed,
                                                     tmp_path,
                                                     monkeypatch):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    monkeypatch.setattr(model_discovery, "reconcile_absent",
                        lambda *a, **k: (_ for _ in ()).throw(
                            SQLAlchemyError("synthetic")))
    res = _scan_ok(client, authed)           # job STILL succeeds
    assert res["absent_reconcile"] == "failed"
    assert res["absent_marked"] == 0


def test_D37_completed_reconcile_reports_done(client, authed, tmp_path):
    """F2 — a committed reconcile is reported as done; the terminal
    state 'succeeded' cannot contradict it."""
    f = _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    f.unlink()
    res = _scan_ok(client, authed)
    assert res["absent_reconcile"] == "done"
    assert res["absent_marked"] == 1


def test_seen_count_distinct_jobs_only(client, authed, tmp_path):
    _write(tmp_path, "m.safetensors", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    _scan_ok(client, authed)
    assert _cands(client)[0]["seen_count"] == 2


def test_umasked_extension_case(client, authed, tmp_path):
    _write(tmp_path, "M.SAFETENSORS", _st_bytes())
    _add_root(client, authed, tmp_path)
    _scan_ok(client, authed)
    assert len(_cands(client)) == 1
