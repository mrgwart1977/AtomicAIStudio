"""C1.5 slice 4 Stage B — opt-in background ImportWatcher polling.

ImportWatchService runs one shared asyncio task; tests drive ``_tick()``
directly through ``asyncio.run`` so timing is fully deterministic — no
real loop waits. The app's own service (started by the lifespan) polls
every 60s and never interferes: no test takes that long, and each test
uses its OWN service instance for assertions.

Invariants under test: off by default, opt-in persisted via audited
commands, one scan per provider in flight, all filesystem work off the
event loop, degraded-on-invalid-folder, pause under memory pressure /
active inference, bounded caches, zero durable audit per tick, and
candidates that can never become assets without an explicit import.
"""
from __future__ import annotations

import asyncio
import itertools
import threading
import time
from pathlib import Path

import pytest

from app.db.models import Asset, AuditEvent, CommandTransaction, GenerationJob
from app.domain import bridge
from app.domain.providers import registry
from app.domain.watch_service import (CACHE_MAX_PER_PROVIDER,
                                      CACHE_MAX_TOTAL,
                                      POLL_INTERVAL_S,
                                      POLL_MIN_INTERVAL_S,
                                      ImportWatchService)
from tests.conftest import make_project, wait_for_job

_KEYS = itertools.count(1)


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, command_type, payload):
    return client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": command_type, "payload": payload,
              "scope": "project", "expected_revision": _rev(client, pid),
              "idempotency_key": f"watch-b-{next(_KEYS):04d}"})


def _sf(client):
    return client.app.state.session_factory


def _configure(client, key="midjourney", folder=None, behavior=None):
    with _sf(client)() as s:
        settings = dict((registry.get_config(s).get(key) or {})
                        .get("settings") or {})
        if folder is not None:
            settings["import_dir"] = str(folder)
        if behavior is not None:
            settings["import_behavior"] = behavior
        registry.set_provider_config(s, key, {"settings": settings})
        s.commit()


def _mk(folder: Path, name: str) -> Path:
    f = folder / name
    f.write_bytes(b"\x89PNG" + name.encode())
    return f


def _tick(svc: ImportWatchService):
    asyncio.run(svc._tick())


def _collect(svc: ImportWatchService, timeout: float = 5.0):
    """Schedule (tick 1), wait for the worker thread to finish, collect
    (tick 2). The wait matters — a just-submitted scan is not done yet
    when the next tick checks it."""
    _tick(svc)
    deadline = time.time() + timeout
    while svc._pending and time.time() < deadline:
        time.sleep(0.01)
    _tick(svc)


class TestEnableDisableCommands:
    """bridge.watch.enable / .disable — audited project commands that flip
    a persisted app-level flag. Off by default; enable refuses against no
    folder."""

    def test_disabled_by_default(self, client):
        p = make_project(client)
        r = client.get("/api/providers/midjourney/watch")
        assert r.status_code == 200
        st = r.json()
        assert st["enabled"] is False and st["status"] == "off"
        assert st["candidates"] == []

    def test_enable_requires_designated_folder(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.watch.enable",
                    {"provider_key": "midjourney"})
        assert r.status_code == 422
        assert "no import folder designated" in r.json()["detail"]

    def test_enable_persists_optin_and_audits(self, client, tmp_path):
        p = make_project(client)
        r = client.put("/api/providers/midjourney/config",
                       json={"settings": {"import_dir": str(tmp_path)}})
        assert r.status_code == 200
        r = _commit(client, p["id"], "bridge.watch.enable",
                    {"provider_key": "midjourney"})
        assert r.status_code == 200, r.text
        b = r.json()["bridge"]
        assert b["action"] == "watch_enable"
        with _sf(client)() as s:
            cfg = registry.get_config(s)["midjourney"]
            assert cfg["settings"]["import_behavior"] == "poll"
            tx = (s.query(CommandTransaction)
                  .order_by(CommandTransaction.created_at.desc()).first())
        assert tx is not None
        assert tx.audit_json["after"]["bridge"]["action"] == "watch_enable"
        # no absolute path in the durable audit record
        assert str(tmp_path) not in str(tx.audit_json)
        # endpoint now reports enabled even before the first tick
        assert client.get(
            "/api/providers/midjourney/watch").json()["enabled"] is True

    def test_disable_persists_and_audits(self, client, tmp_path):
        p = make_project(client)
        client.put("/api/providers/midjourney/config",
                   json={"settings": {"import_dir": str(tmp_path)}})
        _commit(client, p["id"], "bridge.watch.enable",
                {"provider_key": "midjourney"})
        r = _commit(client, p["id"], "bridge.watch.disable",
                    {"provider_key": "midjourney"})
        assert r.status_code == 200, r.text
        assert r.json()["bridge"]["action"] == "watch_disable"
        with _sf(client)() as s:
            cfg = registry.get_config(s)["midjourney"]
            assert cfg["settings"]["import_behavior"] == "manual"
        assert client.get(
            "/api/providers/midjourney/watch").json()["enabled"] is False

    def test_interval_floor(self, client):
        svc = ImportWatchService(_sf(client))
        assert svc._interval == POLL_INTERVAL_S
        assert ImportWatchService(_sf(client), interval_s=5)._interval \
            == POLL_MIN_INTERVAL_S


class TestServicePolling:
    """The shared task itself — scheduling, collection, degraded state,
    pause gates, and dedup. Every scan goes through the SAME Stage A
    helpers as the manual check."""

    def test_disabled_watch_never_touches_fs(self, client, tmp_path,
                                             monkeypatch):
        calls = []
        monkeypatch.setattr(bridge, "scan_watch_folder",
                            lambda *a, **k: calls.append(1) or ([], False))
        _configure(client, folder=tmp_path)          # folder, but NO opt-in
        svc = ImportWatchService(_sf(client))
        _collect(svc)
        assert calls == []
        assert svc.status("midjourney")["status"] == "off"

    def test_opted_in_provider_is_scanned(self, client, tmp_path):
        _mk(tmp_path, "a.png"); _mk(tmp_path, "b.png")
        _mk(tmp_path, "skip.exe")                    # ineligible extension
        _configure(client, folder=tmp_path, behavior="poll")
        svc = ImportWatchService(_sf(client))
        _collect(svc)
        st = svc.status("midjourney")
        assert st["enabled"] and st["status"] == "ok"
        assert st["last_poll"] is not None
        assert {c["name"] for c in st["candidates"]} == {"a.png", "b.png"}
        assert st["folder_name"] == tmp_path.name

    def test_source_files_never_modified(self, client, tmp_path):
        f = _mk(tmp_path, "keep.png")
        before = f.read_bytes()
        _configure(client, folder=tmp_path, behavior="poll")
        svc = ImportWatchService(_sf(client))
        _collect(svc)
        assert f.read_bytes() == before and f.exists()

    def test_candidates_drop_when_file_removed(self, client, tmp_path):
        f = _mk(tmp_path, "gone.png")
        _configure(client, folder=tmp_path, behavior="poll")
        svc = ImportWatchService(_sf(client))
        _collect(svc)
        assert len(svc.status("midjourney")["candidates"]) == 1
        f.unlink()
        _collect(svc)
        assert svc.status("midjourney")["candidates"] == []

    def test_folder_disappears_degraded_then_recovers(self, client,
                                                      tmp_path):
        folder = tmp_path / "watch"
        folder.mkdir()
        _mk(folder, "x.png")
        _configure(client, folder=folder, behavior="poll")
        svc = ImportWatchService(_sf(client))
        _collect(svc)
        assert svc.status("midjourney")["status"] == "ok"
        import shutil
        shutil.rmtree(folder)
        _tick(svc)
        st = svc.status("midjourney")
        assert st["status"] == "degraded"
        assert "unavailable" in st["reason"] or "no longer valid" in st["reason"]
        # recovers only when the ORIGINAL folder validates again
        folder.mkdir()
        _mk(folder, "y.png")
        _collect(svc)
        st = svc.status("midjourney")
        assert st["status"] == "ok"
        assert {c["name"] for c in st["candidates"]} == {"y.png"}

    def test_disable_midflight_drops_state(self, client, tmp_path):
        _mk(tmp_path, "x.png")
        _configure(client, folder=tmp_path, behavior="poll")
        svc = ImportWatchService(_sf(client))
        _collect(svc)
        assert svc.status("midjourney")["status"] == "ok"
        _configure(client, folder=tmp_path, behavior="manual")
        _tick(svc)
        assert svc.status("midjourney")["status"] == "off"
        assert svc.status("midjourney")["candidates"] == []

    def test_no_overlapping_scans(self, client, tmp_path, monkeypatch):
        gate = threading.Event()
        submitted = []

        def slow_scan(*a, **k):
            submitted.append(1)
            gate.wait(5)
            return [], False

        monkeypatch.setattr(bridge, "scan_watch_folder", slow_scan)
        _configure(client, folder=tmp_path, behavior="poll")
        svc = ImportWatchService(_sf(client))
        _tick(svc)                                   # schedules scan 1
        _tick(svc)                                   # still running — skip
        _tick(svc)                                   # still running — skip
        gate.set()
        _tick(svc)                                   # collects finished scan
        assert submitted == [1]                      # exactly one scan ran

    def test_scan_error_marks_degraded_not_crash(self, client, tmp_path,
                                                 monkeypatch):
        monkeypatch.setattr(
            bridge, "scan_watch_folder",
            lambda *a, **k: (_ for _ in ()).throw(OSError("io")))
        _configure(client, folder=tmp_path, behavior="poll")
        svc = ImportWatchService(_sf(client))
        _collect(svc)
        st = svc.status("midjourney")
        assert st["status"] == "degraded"
        # service still alive — a tick failure never kills it
        _tick(svc)

    def test_memory_pressure_pauses_polling(self, client, tmp_path):
        _configure(client, folder=tmp_path, behavior="poll")
        svc = ImportWatchService(_sf(client),
                                 pressure=lambda: "critical")
        _tick(svc)
        st = svc.status("midjourney")
        assert st["paused_reason"] == "system memory pressure"
        # and recovers when pressure clears
        svc._pressure = lambda: "ok"
        _collect(svc)
        assert svc.status("midjourney")["paused_reason"] is None

    def test_active_inference_pauses_polling(self, client, tmp_path):
        _mk(tmp_path, "x.png")
        _configure(client, folder=tmp_path, behavior="poll")
        p = make_project(client)
        with _sf(client)() as s:
            s.add(GenerationJob(
                project_id=p["id"], adapter_id="gen.image.sdcpp",
                kind="image", request_json={}, status="running",
                idempotency_key="watch-test-busy"))
            s.commit()
        svc = ImportWatchService(_sf(client))
        _tick(svc)
        assert svc.status("midjourney")["paused_reason"] \
            == "generation job running"
        assert svc.status("midjourney")["candidates"] == []

    def test_no_durable_audit_per_tick(self, client, tmp_path):
        _configure(client, folder=tmp_path, behavior="poll")
        with _sf(client)() as s:
            before = s.query(CommandTransaction).count()
            audits = s.query(AuditEvent).count()
        svc = ImportWatchService(_sf(client))
        _collect(svc)
        with _sf(client)() as s:
            assert s.query(CommandTransaction).count() == before
            assert s.query(AuditEvent).count() == audits

    def test_restart_rearms_from_persisted_flag(self, client, tmp_path):
        """A NEW service instance (an app restart) picks up the persisted
        opt-in — but only because the flag is read fresh each tick, not
        because files were 'monitored' while stopped."""
        _mk(tmp_path, "x.png")
        _configure(client, folder=tmp_path, behavior="poll")
        svc1 = ImportWatchService(_sf(client))
        _collect(svc1)
        assert svc1.status("midjourney")["status"] == "ok"
        svc2 = ImportWatchService(_sf(client))       # "restart"
        assert svc2.status("midjourney")["status"] == "off"  # fresh state
        _collect(svc2)
        assert svc2.status("midjourney")["status"] == "ok"

    def test_candidate_cache_bounds(self, client):
        svc = ImportWatchService(_sf(client))
        st = svc._state.setdefault(
            "midjourney", {"enabled": True, "status": "ok", "reason": None,
                           "folder_name": "x", "candidates": {},
                           "last_poll": None, "paused_reason": None})
        st["candidates"] = {
            f"/p/{i}.png": {"name": f"{i}.png", "path": f"/p/{i}.png",
                            "size": 1, "mtime": float(i), "kind": "image"}
            for i in range(CACHE_MAX_PER_PROVIDER + 50)}
        svc._enforce_cache_cap()
        assert len(st["candidates"]) == CACHE_MAX_PER_PROVIDER + 50  # per-prov cap is on ingest
        # global cap across providers
        st2 = svc._state.setdefault(
            "kling", {"enabled": True, "status": "ok", "reason": None,
                      "folder_name": "x", "candidates": {},
                      "last_poll": None, "paused_reason": None})
        st2["candidates"] = {
            f"/q/{i}.png": {"name": f"{i}.png", "path": f"/q/{i}.png",
                            "size": 1, "mtime": float(10000 + i),
                            "kind": "image"}
            for i in range(CACHE_MAX_TOTAL)}
        svc._enforce_cache_cap()
        total = sum(len(s["candidates"]) for s in svc._state.values())
        assert total <= CACHE_MAX_TOTAL
        # newest kept: kling's all-newer entries survive over stale ones
        assert len(st2["candidates"]) > 0

    def test_symlink_candidate_rejected_via_poll(self, client, tmp_path):
        outside = tmp_path / "outside"; outside.mkdir()
        _mk(outside, "secret.png")
        folder = tmp_path / "watch"; folder.mkdir()
        _mk(folder, "ok.png")
        try:
            (folder / "link.png").symlink_to(outside / "secret.png")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this host")
        _configure(client, folder=folder, behavior="poll")
        svc = ImportWatchService(_sf(client))
        _collect(svc)
        names = {c["name"] for c in svc.status("midjourney")["candidates"]}
        assert names == {"ok.png"}


class TestWatchEndpoint:
    """GET /api/providers/{key}/watch — live service state + persisted
    flag + per-project dedup."""

    def _seed(self, client, folder):
        _mk(folder, "cand.png")
        _configure(client, folder=folder, behavior="poll")
        svc = client.app.state.watch_service
        _collect(svc)

    def test_status_reflects_service(self, client, tmp_path):
        self._seed(client, tmp_path)
        r = client.get("/api/providers/midjourney/watch")
        assert r.status_code == 200
        st = r.json()
        assert st["enabled"] is True and st["status"] == "ok"
        assert [c["name"] for c in st["candidates"]] == ["cand.png"]

    def test_project_dedup_after_import(self, client, tmp_path):
        self._seed(client, tmp_path)
        p = make_project(client)
        st = client.get(
            f"/api/providers/midjourney/watch?project_id={p['id']}"
        ).json()
        assert len(st["candidates"]) == 1
        src = st["candidates"][0]["path"]
        r = _commit(client, p["id"], "bridge.import", {
            "provider_key": "midjourney", "source_path": src,
            "kind": "image", "name": "cand.png", "from_watch": True})
        assert r.status_code == 200, r.text
        job = wait_for_job(client, p["id"], r.json()["job_id"])
        assert job["status"] == "succeeded"
        st = client.get(
            f"/api/providers/midjourney/watch?project_id={p['id']}"
        ).json()
        assert st["candidates"] == []               # already imported — gone
        # provenance unchanged by the polling path
        with _sf(client)() as s:
            a = s.query(Asset).filter_by(
                project_id=p["id"], origin="external").first()
        assert a is not None
        assert a.provenance_json["provenance_confidence"] == "user_reported"
        assert a.provenance_json["external_provider"] == "midjourney"

    def test_project_isolation_no_leak(self, client, tmp_path):
        self._seed(client, tmp_path)
        p1 = make_project(client, "P1"); p2 = make_project(client, "P2")
        st = client.get(
            f"/api/providers/midjourney/watch?project_id={p1['id']}"
        ).json()
        src = st["candidates"][0]["path"]
        r = _commit(client, p1["id"], "bridge.import", {
            "provider_key": "midjourney", "source_path": src,
            "kind": "image", "name": "cand.png", "from_watch": True})
        wait_for_job(client, p1["id"], r.json()["job_id"])
        # dedup is per project — p2 still sees the candidate
        st2 = client.get(
            f"/api/providers/midjourney/watch?project_id={p2['id']}"
        ).json()
        assert len(st2["candidates"]) == 1


class TestLifecycle:
    def test_start_stop_cancels_task(self, client):
        svc = ImportWatchService(_sf(client), interval_s=POLL_MIN_INTERVAL_S)

        async def go():
            await svc.start()
            assert svc._task is not None and not svc._task.done()
            await svc.stop()
            assert svc._task is None
        asyncio.run(go())

    def test_stop_waits_for_inflight_scan(self, client, tmp_path,
                                        monkeypatch):
        gate = threading.Event()
        ran = []

        def slow(*a, **k):
            gate.wait(5)
            ran.append(1)
            return [], False

        monkeypatch.setattr(bridge, "scan_watch_folder", slow)
        _configure(client, folder=tmp_path, behavior="poll")
        svc = ImportWatchService(_sf(client))
        _tick(svc)                                   # schedules slow scan
        threading.Timer(0.2, gate.set).start()       # release off-loop
        t0 = time.monotonic()
        asyncio.run(svc.stop())                      # must wait it out
        assert ran == [1] and time.monotonic() - t0 < 4
