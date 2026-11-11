"""C1.5 — Universal AI Access Bridge: enriched strategies, freshness
demotion, adapter-kind honesty, and external-import provenance tagging.

No provider is contacted in any test — the bridge is registry data,
import jobs and provenance marking only (C1.5 §11 invariants).
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from app.adapters.base import ADAPTER_KINDS
from app.domain.providers import catalog, registry
from tests.conftest import make_project, wait_for_job


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _import(client, pid, path: Path, extra=None):
    rev = _rev(client, pid)
    payload = {"source_path": str(path)}
    if extra:
        payload.update(extra)
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": "asset.import", "payload": payload,
              "scope": "project", "expected_revision": rev,
              "idempotency_key": f"imp-{path.name}-{rev}-{len(payload)}"})
    assert r.status_code == 200, r.text
    return wait_for_job(client, pid, r.json()["job_id"])


def _assets(client, pid):
    return client.get(f"/api/projects/{pid}/assets").json()["items"]


# ---------------- catalog integrity ----------------

class TestCatalogIntegrity:
    def test_validate_catalog_clean(self):
        assert catalog.validate_catalog() == []

    def test_60_providers_unique_strategy_ids(self):
        assert catalog.catalog_count() == 60
        ids = [s["strategy_id"] for p in catalog.all_providers()
               for s in p["strategies"]]
        assert len(ids) == len(set(ids)) and all(ids)

    def test_tos_evidence_mandatory_non_official(self):
        for p in catalog.all_providers():
            for s in p["strategies"]:
                if s["method"] == "official_api":
                    continue
                ev = s["tos_evidence"]
                assert ev.get("url") and ev.get("checked"), (
                    f"{s['strategy_id']} missing tos_evidence")

    def test_strategy_record_shape(self):
        s = catalog.get_provider("suno")["strategies"][0]
        for field in ("strategy_id", "provider_id", "method", "tier",
                      "capabilities", "auth_method", "user_interaction",
                      "automation_permission", "tos_evidence",
                      "input_formats", "output_formats", "settings_schema",
                      "cost_implications", "last_verified", "note"):
            assert field in s, f"missing §3 field {field}"

    def test_method_tier_consistency(self):
        for p in catalog.all_providers():
            for s in p["strategies"]:
                if s["automation_permission"] == "prohibited":
                    assert s["tier"] == "T4"
                else:
                    assert s["tier"] in catalog.METHOD_TIERS[s["method"]]


# ---------------- prohibited & freshness ----------------

class TestProhibitedAndFreshness:
    def test_prohibited_never_best(self):
        mj = catalog.get_provider("midjourney")
        refused = [s for s in mj["strategies"]
                   if s["automation_permission"] == "prohibited"]
        assert refused, "Midjourney should record the refused MCP path"
        assert registry.best_strategy(mj)["method"] == "assisted_workflow"
        assert all(s["automation_permission"] != "prohibited"
                   for s in registry.usable_strategies(mj))

    def test_stale_demotes_status(self):
        prov = dict(catalog.get_provider("sudowrite"))
        old = {"last_verified": "2020-01-01",
               "tos_evidence": {"url": "x", "checked": "2020-01-01"}}
        prov["strategies"] = [{**s, **old} for s in prov["strategies"]]
        st = registry.compute_status(prov, None, enabled=False)
        assert st["status"] == "needs_recheck"

    def test_fresh_strategy_not_stale(self):
        prov = catalog.get_provider("sudowrite")
        today = datetime.date.fromisoformat(catalog.CATALOG_VERIFIED)
        assert not registry.strategy_stale(prov, today)
        st = registry.compute_status(prov, None, enabled=False, today=today)
        assert st["status"] != "needs_recheck"

    def test_missing_date_is_stale(self):
        prov = dict(catalog.get_provider("sudowrite"))
        prov["strategies"] = [{**s, "last_verified": None,
                               "tos_evidence": {}} for s in prov["strategies"]]
        assert registry.strategy_stale(prov)

    def test_t4_never_stale(self):
        assert not registry.strategy_stale(catalog.get_provider("udio"))

    def test_enable_blocked_when_stale(self, client, monkeypatch):
        monkeypatch.setattr(registry, "strategy_stale", lambda *a, **k: True)
        r = client.put("/api/providers/sudowrite/config",
                       json={"enabled": True})
        assert r.status_code == 422 and "stale" in r.text.lower()

    def test_enable_ok_when_fresh(self, client, monkeypatch):
        monkeypatch.setattr(registry, "strategy_stale", lambda *a, **k: False)
        r = client.put("/api/providers/sudowrite/config",
                       json={"enabled": True})
        assert r.status_code == 200


# ---------------- adapter-kind axis ----------------

def test_adapter_kinds_include_cloud_tiers():
    assert {"mock", "local", "cloud", "cloud-assisted",
            "cloud-external"} <= ADAPTER_KINDS


# ---------------- external-source provenance ----------------

class TestExternalProvenance:
    def test_external_source_tags_provenance(self, client, tmp_path):
        p = make_project(client)
        f = tmp_path / "song.mp3"
        f.write_bytes(b"fake-mp3-bytes")
        job = _import(client, p["id"], f, {
            "external_source": {"provider_key": "suno",
                                "strategy_id": "suno.mcp_connector.v1",
                                "note": "downloaded from my Suno library"}})
        assert job["status"] == "succeeded", job.get("error")
        asset = _assets(client, p["id"])[0]
        assert asset["origin"] == "external"
        prov = asset["provenance_json"]
        assert prov["provenance_confidence"] == "user_reported"
        assert prov["external_provider"] == "suno"
        assert prov["external_strategy"] == "suno.mcp_connector.v1"
        assert prov["external_note"] == "downloaded from my Suno library"
        assert prov["external_reported_at"]
        assert "verified_remote" not in prov

    def test_method_inferred_when_omitted(self, client, tmp_path):
        p = make_project(client)
        f = tmp_path / "img.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        job = _import(client, p["id"], f, {
            "external_source": {"provider_key": "midjourney"}})
        assert job["status"] == "succeeded", job.get("error")
        prov = _assets(client, p["id"])[0]["provenance_json"]
        assert prov["external_provider"] == "midjourney"
        assert prov["provenance_confidence"] == "user_reported"

    def test_unknown_provider_rejected(self, client, tmp_path):
        p = make_project(client)
        f = tmp_path / "x.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        job = _import(client, p["id"], f, {
            "external_source": {"provider_key": "not-a-provider"}})
        assert job["status"] == "failed"
        assert "provider" in (job.get("error") or "").lower()

    def test_foreign_strategy_id_rejected(self, client, tmp_path):
        p = make_project(client)
        f = tmp_path / "x.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        job = _import(client, p["id"], f, {
            "external_source": {"provider_key": "suno",
                                "strategy_id": "midjourney.assisted_workflow.v1"}})
        assert job["status"] == "failed"

    def test_bad_method_rejected(self, client, tmp_path):
        p = make_project(client)
        f = tmp_path / "x.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        job = _import(client, p["id"], f, {
            "external_source": {"provider_key": "suno",
                                "method": "telegram_bot"}})
        assert job["status"] == "failed"

    def test_plain_import_unchanged(self, client, tmp_path):
        p = make_project(client)
        f = tmp_path / "plain.txt"
        f.write_bytes(b"local file")
        job = _import(client, p["id"], f)
        assert job["status"] == "succeeded"
        asset = _assets(client, p["id"])[0]
        assert asset["origin"] == "imported"
        assert "external_provider" not in asset["provenance_json"]
        assert "provenance_confidence" not in asset["provenance_json"]
