"""C1.5 slice 2 — Bridge dispatch + per-feature capability reporting.

Every dispatch is a command (preview → commit → audit) and every action is
user-driven: no provider is contacted, no browser automation runs, and no
API call is ever made in these tests. webbrowser.open is monkeypatched —
the tests assert intent, URL, and audit, never an actual open.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from app.domain import bridge
from app.domain.providers import catalog, registry
from tests.conftest import make_project, wait_for_job


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


import itertools

_KEYS = itertools.count(1)


def _commit(client, pid, command_type, payload, rev=None, key=None):
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": command_type, "payload": payload,
              "scope": "project", "expected_revision": _rev(client, pid) if rev is None else rev,
              "idempotency_key": key or f"test-key-{next(_KEYS):04d}"})
    return r


def _preview(client, pid, command_type, payload):
    return client.post(
        f"/api/projects/{pid}/commands/preview",
        json={"command_type": command_type, "payload": payload,
              "scope": "project"})


# ---------------- resolve_strategy ----------------

class TestResolveStrategy:
    def test_t2_provider_resolves_assisted(self):
        p = catalog.get_provider("midjourney")
        s, err = bridge.resolve_strategy(p, "launch")
        assert err is None and s["method"] == "assisted_workflow"

    def test_monitor_only_refused(self):
        s, err = bridge.resolve_strategy(catalog.get_provider("udio"), "import")
        assert s is None and "monitor-only" in err

    def test_t1_only_launch_refused(self):
        s, err = bridge.resolve_strategy(catalog.get_provider("openai"), "launch")
        assert s is None and "T1" in err

    def test_t1_plus_t3_import_resolves_import_export(self):
        p = catalog.get_provider("descript")
        s, err = bridge.resolve_strategy(p, "import")
        assert err is None and s["method"] == "import_export"

    def test_prohibited_strategy_id_refused(self):
        p = catalog.get_provider("midjourney")
        prohibited = next(s for s in p["strategies"]
                          if s["automation_permission"] == "prohibited")
        s, err = bridge.resolve_strategy(p, "launch", prohibited["strategy_id"])
        assert s is None and "prohibited" in err

    def test_unknown_strategy_id_refused(self):
        s, err = bridge.resolve_strategy(
            catalog.get_provider("midjourney"), "launch", "bogus.id.v9")
        assert s is None and "not found" in err

    def test_t1_strategy_id_never_dispatched(self):
        # Even explicitly selecting a T1 record must refuse — the bridge
        # performs no programmatic calls in C1.5 (no silent fallback).
        p = catalog.get_provider("suno")
        t1 = next(s for s in p["strategies"] if s["tier"] == "T1")
        s, err = bridge.resolve_strategy(p, "prepare", t1["strategy_id"])
        assert s is None and "not implemented" in err

    def test_auto_resolve_prefers_user_driven(self):
        # Suno's best strategy is T1 MCP, but dispatch must pick the
        # user-driven T2 record — never the unattended channel.
        s, err = bridge.resolve_strategy(catalog.get_provider("suno"), "prepare")
        assert err is None and s["method"] == "assisted_workflow"


# ---------------- launch URL validation ----------------

class TestLaunchUrl:
    def _prov(self, **kw):
        p = {"key": "x", "label": "X", "strategies": [],
             "docs_url": None, "tos_url": None}
        p.update(kw)
        return p

    def test_connection_launch_url_wins(self):
        p = self._prov(docs_url="https://docs.x.com")
        s = {"connection": {"launch_url": "https://app.x.com"}}
        assert bridge.resolve_launch_url(p, s) == "https://app.x.com"

    def test_docs_then_strategy_evidence_then_tos(self):
        p = self._prov(tos_url="https://x.com/terms")
        assert bridge.resolve_launch_url(p, {}) == "https://x.com/terms"
        p2 = self._prov(docs_url="https://docs.x.com",
                        tos_url="https://x.com/terms")
        assert bridge.resolve_launch_url(p2, {}) == "https://docs.x.com"
        p3 = self._prov()
        s3 = {"tos_evidence": {"url": "https://app.x.com"}}
        assert bridge.resolve_launch_url(p3, s3) == "https://app.x.com"

    def test_unsafe_schemes_rejected(self):
        for bad in ("http://x.com", "javascript:alert(1)",
                    "file:///etc/passwd", "ftp://x.com", "notaurl"):
            p = self._prov(docs_url=bad, tos_url=bad)
            s = {"connection": {"launch_url": bad},
                 "tos_evidence": {"url": bad}}
            assert bridge.resolve_launch_url(p, s) is None, bad

    def test_no_surface_returns_none(self):
        assert bridge.resolve_launch_url(self._prov(), {}) is None

    def test_catalog_t2_providers_have_verified_hosts(self):
        # Every launchable provider resolves a catalog URL — none comes
        # from user input.
        for p in catalog.all_providers():
            s, _ = bridge.resolve_strategy(p, "launch")
            if s is None:
                continue
            url = bridge.resolve_launch_url(p, s)
            if url is None:
                continue  # hemingway-class: honestly refuses
            assert url.startswith("https://"), (p["key"], url)


# ---------------- capability report ----------------

class TestCapabilityReport:
    def test_t2_capability_dispatchable(self):
        r = bridge.capability_report(catalog.get_provider("midjourney"))
        cap = next(c for c in r["capabilities"]
                   if c["capability"] == "image.generate")
        assert cap["tier"] == "T2"
        assert cap["method"] == "assisted_workflow"
        assert cap["dispatch"] == ["prepare", "launch", "import"]
        assert cap["status"] == "documented"
        assert cap["dispatch_blocked"] is None

    def test_t1_never_claimed_operational(self):
        r = bridge.capability_report(catalog.get_provider("suno"))
        cap = r["capabilities"][0]
        assert cap["tier"] == "T1"
        assert cap["dispatch"] == []
        assert cap["dispatch_blocked"] == "t1_not_implemented"
        assert cap["alternate"]["method"] == "assisted_workflow"

    def test_every_entry_labeled_catalog_documented(self):
        for p in catalog.all_providers():
            for cap in bridge.capability_report(p)["capabilities"]:
                assert cap["verification"] == "catalog_documented", (
                    p["key"], cap["capability"])

    def test_prohibited_only_capability_refused(self):
        fake = {"key": "x", "strategies": [
            {"method": "mcp_connector", "tier": "T4",
             "capabilities": ["image.generate"],
             "automation_permission": "prohibited"}]}
        cap = bridge.capability_report(fake)["capabilities"][0]
        assert cap["status"] == "refused_prohibited"
        assert cap["dispatch_blocked"] == "prohibited"
        assert cap["required_user_action"] == "none_available"

    def test_stale_capability_needs_recheck(self, monkeypatch):
        monkeypatch.setattr(registry, "STRATEGY_FRESH_DAYS", -1)
        r = bridge.capability_report(catalog.get_provider("midjourney"))
        cap = r["capabilities"][0]
        assert cap["status"] == "needs_recheck" and cap["stale"]
        assert cap["dispatch_blocked"] == "stale"
        assert cap["required_user_action"] == "reverify_strategy"

    def test_t4_provider_reports_no_dispatch(self):
        r = bridge.capability_report(catalog.get_provider("udio"))
        assert r["capabilities"] == []  # monitor-only, nothing claimed

    def test_report_via_endpoint(self, client):
        r = client.get("/api/providers/midjourney/capabilities")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider_key"] == "midjourney"
        assert body["provider_status"]
        assert body["evidence_stamp"] == catalog.CATALOG_VERIFIED
        assert body["capabilities"][0]["verification"] == "catalog_documented"

    def test_capabilities_endpoint_404(self, client):
        assert client.get("/api/providers/nope/capabilities").status_code == 404


# ---------------- bridge.prepare ----------------

class TestBridgePrepare:
    def test_preview_free_and_warns(self, client):
        p = make_project(client)
        r = _preview(client, p["id"], "bridge.prepare",
                     {"provider_key": "midjourney", "prompt": "a lighthouse"})
        assert r.status_code == 200, r.text
        w = " ".join(r.json()["warnings"]).lower()
        assert "paste" in w and "terms" in w
        assert r.json()["launches_generation"] is False

    def test_commit_returns_prepared_artifact(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.prepare",
                    {"provider_key": "midjourney", "prompt": "a lighthouse",
                     "negative_prompt": "fog"})
        assert r.status_code == 200, r.text
        b = r.json()["bridge"]
        assert b["action"] == "prepare"
        assert b["prepared_text"] == "a lighthouse\n\nNegative: fog"
        assert b["strategy_id"] == "midjourney.assisted_workflow.v1"
        assert b["template_applied"] is False
        assert "paste" in b["paste_hint"].lower()

    def test_prompt_template_applied(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.prepare",
                    {"provider_key": "midjourney", "prompt": "a lighthouse",
                     "provider_settings": {
                         "prompt_template": "{prompt} --ar 16:9 --v 7"}})
        b = r.json()["bridge"]
        assert b["prepared_text"] == "a lighthouse --ar 16:9 --v 7"
        assert b["template_applied"] is True

    def test_prepare_audit_trail(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.prepare",
                    {"provider_key": "midjourney", "prompt": "x"})
        assert r.status_code == 200
        cmds = client.get(f"/api/projects/{p['id']}/commands").json()
        row = next(c for c in cmds if c["command_type"] == "bridge.prepare")
        assert row["status"] == "committed"

    def test_prepare_requires_prompt(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.prepare",
                    {"provider_key": "midjourney", "prompt": "  "})
        assert r.status_code == 422

    def test_prepare_t1_provider_refused(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.prepare",
                    {"provider_key": "openai", "prompt": "hi"})
        assert r.status_code == 422
        assert "T2" in r.json()["detail"] or "T1" in r.json()["detail"]


# ---------------- bridge.launch ----------------

class TestBridgeLaunch:
    def test_backend_cannot_open_browser(self):
        # The launch split is structural, not a policy flag: the module
        # contains no mechanism to spawn a browser at all — no job, replay,
        # or API request can produce a window server-side.
        import inspect
        src = inspect.getsource(bridge)
        for forbidden in ("import webbrowser", "webbrowser.open",
                          "import subprocess", "subprocess.run",
                          "subprocess.Popen", "os.system"):
            assert forbidden not in src, forbidden
        # and no launch helper survives to be invoked
        assert not hasattr(bridge, "_open_url")
        assert not hasattr(bridge, "launch")

    def test_commit_delivers_validated_url_to_client(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.launch",
                    {"provider_key": "midjourney"})
        assert r.status_code == 200, r.text
        b = r.json()["bridge"]
        assert b["url"] == "https://www.midjourney.com"
        assert b["open_via"] == "client_shell"  # desktop opens it explicitly

    def test_payload_url_never_honored(self, client):
        # "Do not accept arbitrary launch URLs" — the payload field is
        # ignored entirely; only catalog URLs can be delivered.
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.launch",
                    {"provider_key": "midjourney",
                     "url": "https://evil.example/phish"})
        assert r.status_code == 200, r.text
        assert r.json()["bridge"]["url"] == "https://www.midjourney.com"
        assert "evil.example" not in r.text

    def test_launch_audited_in_command_row(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.launch",
                    {"provider_key": "midjourney"})
        assert r.status_code == 200, r.text
        cmds = client.get(f"/api/projects/{p['id']}/commands").json()
        row = next(c for c in cmds if c["command_type"] == "bridge.launch")
        assert row["status"] == "committed"
        assert row["payload_json"]["provider_key"] == "midjourney"

    def test_idempotent_replay_not_redispatched(self, client):
        p = make_project(client)
        rev = _rev(client, p["id"])
        r1 = _commit(client, p["id"], "bridge.launch",
                     {"provider_key": "suno"}, rev=rev, key="launch-1")
        r2 = _commit(client, p["id"], "bridge.launch",
                     {"provider_key": "suno"}, rev=rev, key="launch-1")
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json()["idempotent_replay"] is True
        cmds = client.get(f"/api/projects/{p['id']}/commands").json()
        assert sum(1 for c in cmds
                   if c["command_type"] == "bridge.launch") == 1

    def test_launch_monitor_only_refused(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.launch",
                    {"provider_key": "udio"})
        assert r.status_code == 422
        assert "monitor-only" in r.json()["detail"]

    def test_launch_t1_provider_refused(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.launch",
                    {"provider_key": "openai"})
        assert r.status_code == 422

    def test_launch_stale_refused(self, client, monkeypatch):
        monkeypatch.setattr(registry, "STRATEGY_FRESH_DAYS", -1)
        p = make_project(client)
        r = _preview(client, p["id"], "bridge.launch",
                     {"provider_key": "midjourney"})
        assert r.status_code == 422
        assert "stale" in r.json()["detail"]


# ---------------- bridge.import ----------------

class TestBridgeImport:
    def test_file_import_external_provenance(self, client, tmp_path):
        f = tmp_path / "out.png"
        f.write_bytes(b"\x89PNG fake")
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.import",
                    {"provider_key": "midjourney", "source_path": str(f),
                     "kind": "image", "note": "downloaded after web run"})
        assert r.status_code == 200, r.text
        job = wait_for_job(client, p["id"], r.json()["job_id"])
        assert job["status"] == "succeeded", job
        assets = client.get(f"/api/projects/{p['id']}/assets").json()["items"]
        prov = assets[0]["provenance_json"]
        assert assets[0]["origin"] == "external"
        assert prov["provenance_confidence"] == "user_reported"
        assert prov["external_provider"] == "midjourney"
        assert prov["external_method"] == "assisted_workflow"
        assert prov["external_strategy"] == "midjourney.assisted_workflow.v1"
        assert prov["external_note"] == "downloaded after web run"
        assert r.json()["bridge"]["action"] == "import"
        assert r.json()["bridge"]["job_id"] == job["id"]

    def test_content_import_cleans_tempfile(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.import",
                    {"provider_key": "quillbot",
                     "content": "rewritten paragraph text",
                     "kind": "other"})
        assert r.status_code == 200, r.text
        job = wait_for_job(client, p["id"], r.json()["job_id"])
        assert job["status"] == "succeeded", job
        assets = client.get(f"/api/projects/{p['id']}/assets").json()["items"]
        assert assets[0]["origin"] == "external"
        assert assets[0]["name"] == "clipboard-import-quillbot.txt"
        prov = assets[0]["provenance_json"]
        assert prov["provenance_confidence"] == "user_reported"
        # The clipboard tempfile was materialized only for ingest and must
        # be deleted afterwards — no user content is left in temp dirs.
        tmp = Path(prov["request"]["source_path"])
        assert tmp.name.startswith("ade-bridge-")
        assert not tmp.exists()

    def test_enqueue_failure_cleans_tempfile(self, client, monkeypatch):
        import tempfile as _tf
        from app.domain.jobs import OpBusyError
        made = []
        real_mkstemp = _tf.mkstemp

        def spy(*a, **k):
            fd, path = real_mkstemp(*a, **k)
            made.append(path)
            return fd, path

        monkeypatch.setattr(_tf, "mkstemp", spy)
        engine = client.app.state.job_engine

        def boom(*a, **k):
            raise OpBusyError("ops engine busy")

        monkeypatch.setattr(engine, "enqueue", boom)
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.import",
                    {"provider_key": "quillbot", "content": "x"})
        assert r.status_code == 409, r.text
        assert made, "tempfile was never materialized"
        assert all(not Path(m).exists() for m in made)

    def test_ingest_failure_cleans_tempfile(self, client, monkeypatch):
        import tempfile as _tf
        made = []
        real_mkstemp = _tf.mkstemp

        def spy(*a, **k):
            fd, path = real_mkstemp(*a, **k)
            made.append(path)
            return fd, path

        monkeypatch.setattr(_tf, "mkstemp", spy)
        storage = client.app.state.storage

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(storage, "ingest", boom)
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.import",
                    {"provider_key": "quillbot", "content": "x"})
        assert r.status_code == 200, r.text
        job = wait_for_job(client, p["id"], r.json()["job_id"])
        assert job["status"] == "failed", job
        assert all(not Path(m).exists() for m in made)

    def test_user_file_never_deleted(self, client, tmp_path):
        # delete_source_after must not be weaponizable via plain
        # asset.import — the ownership check requires tempdir + prefix.
        f = tmp_path / "mine.png"
        f.write_bytes(b"mine")
        p = make_project(client)
        r = _commit(client, p["id"], "asset.import",
                    {"source_path": str(f), "delete_source_after": True})
        assert r.status_code == 200, r.text
        job = wait_for_job(client, p["id"], r.json()["job_id"])
        assert job["status"] == "succeeded"
        assert f.exists()  # user file survives

        # Same guarantee through bridge.import file mode — no delete flag.
        f2 = tmp_path / "mine2.png"
        f2.write_bytes(b"mine2")
        r = _commit(client, p["id"], "bridge.import",
                    {"provider_key": "midjourney", "source_path": str(f2)})
        assert r.status_code == 200, r.text
        wait_for_job(client, p["id"], r.json()["job_id"])
        assert f2.exists()

    def test_import_requires_exactly_one_source(self, client, tmp_path):
        p = make_project(client)
        f = tmp_path / "a.txt"
        f.write_text("x")
        for payload in (
                {"provider_key": "midjourney"},
                {"provider_key": "midjourney", "source_path": str(f),
                 "content": "x"}):
            r = _commit(client, p["id"], "bridge.import", payload)
            assert r.status_code == 422, (payload, r.text)

    def test_import_missing_file_422(self, client):
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.import",
                    {"provider_key": "midjourney",
                     "source_path": "C:/nope/missing.png"})
        assert r.status_code == 422

    def test_import_t1_only_provider_refused(self, client, tmp_path):
        # OpenAI has no documented user-driven path — refuse rather than
        # tag the file as produced via its API.
        f = tmp_path / "out.png"
        f.write_bytes(b"x")
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.import",
                    {"provider_key": "openai", "source_path": str(f)})
        assert r.status_code == 422
        assert "user-driven" in r.json()["detail"]

    def test_import_monitor_only_refused(self, client, tmp_path):
        f = tmp_path / "out.mp3"
        f.write_bytes(b"x")
        p = make_project(client)
        r = _commit(client, p["id"], "bridge.import",
                    {"provider_key": "udio", "source_path": str(f)})
        assert r.status_code == 422

    def test_import_stale_refused(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(registry, "STRATEGY_FRESH_DAYS", -1)
        f = tmp_path / "out.png"
        f.write_bytes(b"x")
        p = make_project(client)
        r = _preview(client, p["id"], "bridge.import",
                     {"provider_key": "midjourney", "source_path": str(f)})
        assert r.status_code == 422
        assert "stale" in r.json()["detail"]

    def test_preview_creates_no_job_or_tempfile(self, client, tmp_path):
        p = make_project(client)
        f = tmp_path / "out.png"
        f.write_bytes(b"x")
        r = _preview(client, p["id"], "bridge.import",
                     {"provider_key": "midjourney", "source_path": str(f)})
        assert r.status_code == 200
        jobs = client.get(f"/api/projects/{p['id']}/jobs").json()
        assert jobs == []


# ---------------- isolation + honesty ----------------

class TestIsolation:
    def test_commands_scoped_to_project(self, client):
        a = make_project(client, "A")
        b = make_project(client, "B")
        _commit(client, a["id"], "bridge.prepare",
                {"provider_key": "midjourney", "prompt": "x"})
        cmds_a = client.get(f"/api/projects/{a['id']}/commands").json()
        cmds_b = client.get(f"/api/projects/{b['id']}/commands").json()
        assert any(c["command_type"] == "bridge.prepare" for c in cmds_a)
        assert not any(c["command_type"].startswith("bridge.") for c in cmds_b)

    def test_bridge_warnings_on_every_dispatch(self, client):
        # Every accepted dispatch must carry the ToS + user-driven warning —
        # nothing may look like an unattended integration.
        p = make_project(client)
        for ctype, payload in (
                ("bridge.prepare", {"provider_key": "midjourney", "prompt": "x"}),
                ("bridge.launch", {"provider_key": "suno"})):
            r = _preview(client, p["id"], ctype, payload)
            assert r.status_code == 200, (ctype, r.text)
            assert any("terms" in w.lower() for w in r.json()["warnings"])


# ---- desktop opener scope ↔ catalog sync (slice 3) ------------------------
# The Tauri capability allow-list is host-scoped to exactly the destinations
# bridge.launch can resolve. Any catalog change to a launch surface must
# update capabilities/default.json in the same PR — enforced here in both
# directions so neither side silently drifts.

import json as _json
from urllib.parse import urlparse as _urlparse

_CAPS_PATH = (Path(__file__).resolve().parents[2]
              / "apps/desktop/src-tauri/capabilities/default.json")


def _resolvable_hosts() -> set[str]:
    hosts: set[str] = set()
    for p in catalog.PROVIDERS:
        for s in p.get("strategies") or []:
            if s.get("automation_permission") == "prohibited":
                continue
            url = bridge.resolve_launch_url(p, s)
            if url:
                hosts.add(_urlparse(url).netloc.lower())
    return hosts


def _allow_entries() -> list[str]:
    caps = _json.loads(_CAPS_PATH.read_text(encoding="utf-8"))
    perm = next(p for p in caps["permissions"]
                if isinstance(p, dict)
                and p.get("identifier") == "opener:allow-open-url")
    return [e["url"] for e in perm["allow"]]


class TestOpenerScopeSync:
    def test_every_resolvable_launch_host_is_allowed(self):
        allowed = set(_allow_entries())
        for h in _resolvable_hosts():
            assert f"https://{h}" in allowed, \
                f"catalog launch host {h} missing from capability allow-list"
            assert f"https://{h}/**" in allowed, \
                f"catalog launch host {h} missing path glob"

    def test_allow_list_admits_only_catalog_hosts(self):
        hosts = _resolvable_hosts()
        for url in _allow_entries():
            assert url.startswith("https://"), \
                f"non-https allow entry: {url}"
            host = _urlparse(url.removesuffix("/**")).netloc.lower()
            assert host in hosts, \
                f"allow-list grants non-catalog host {host}"
            # The authority component must never contain wildcards —
            # https://host.com** would also match host.com.evil.com.
            assert "*" not in host, f"wildcard in authority: {url}"
            # Only two admissible shapes: exact host, or host + any path.
            assert url == f"https://{host}" or url == f"https://{host}/**", \
                f"unexpected allow shape: {url}"

    def test_no_shell_or_path_open_permissions(self):
        caps = _json.loads(_CAPS_PATH.read_text(encoding="utf-8"))
        ids = [p if isinstance(p, str) else p.get("identifier", "")
               for p in caps["permissions"]]
        for pid in ids:
            assert not pid.startswith("shell:"), pid
            assert "open-path" not in pid and "execute" not in pid, pid
