"""C1 — Unified AI Provider Manager tests.

Covers: catalog completeness/integrity, account normalization, settings
validation, capability restrictions, DPAPI credential vault (encrypt/
roundtrip/isolation/removal/unreadable), no-secret-leakage, status
reporting, per-project overrides, mock connection behavior, and
backward compatibility. No live provider contact anywhere.
"""

from __future__ import annotations

import base64
import json
import os

import pytest

from app.domain.providers import catalog, registry
from app.domain.providers.credentials import CredentialVault
from tests.conftest import make_project

IS_WINDOWS = os.name == "nt"
win_only = pytest.mark.skipif(not IS_WINDOWS, reason="DPAPI is Windows-only")


# ================================================================ catalog integrity

class TestCatalog:
    def test_all_60_entries_present(self):
        assert catalog.catalog_count() == 60
        for cat in ("text", "image", "video", "audio"):
            assert sum(1 for p in catalog.PROVIDERS if p["category"] == cat) == 15

    def test_unique_keys_and_valid_enums(self):
        keys = [p["key"] for p in catalog.PROVIDERS]
        assert len(keys) == len(set(keys))
        for p in catalog.PROVIDERS:
            assert p["access_class"] in catalog.ACCESS_CLASSES
            assert p["category"] in catalog.CATEGORIES
            for s in p["strategies"]:
                assert s["tier"] in catalog.TIERS
                assert s["method"] in catalog.METHODS

    def test_every_entry_has_strategy_and_status_path(self):
        for p in catalog.PROVIDERS:
            assert p["strategies"], p["key"]
            # T1 providers must declare credentials or explicitly need none
            strat = registry.best_strategy(p)
            if strat is None:  # monitor-only providers (Udio) have no usable path
                continue
            if strat["tier"] == "T1" and registry.credential_required(p):
                assert p["credential_fields"], p["key"]

    def test_normalization_shared_accounts(self):
        acc = catalog.accounts()
        # 60 rows → 51 audited unique accounts
        assert len(acc) == 51
        for grp in (["openai", "openai-images"],
                    ["google-gemini", "gemini-images", "veo"],
                    ["xai", "grok-imagine"],
                    ["leonardo", "lucid-origin"],
                    ["qwen-image", "wan", "juan-prime"],
                    ["adobe-firefly", "firefly-video"],
                    ["higgsfield", "happy-horse"]):
            acct = catalog.account_of(grp[0])
            assert all(catalog.account_of(k) == acct for k in grp), grp

    def test_ambiguous_names_resolve(self):
        jp = catalog.get_provider("juan-prime")
        assert jp["access_class"] == "E" and "Wan" in jp["status_reason"]
        assert "wan3.0-video-prime" in jp["aliases"]
        mx = catalog.get_provider("miniax")
        assert "minimax" in mx["aliases"]

    def test_no_provider_missing_docs_or_reason(self):
        for p in catalog.PROVIDERS:
            if p["access_class"] in ("D", "E"):
                assert p["status_reason"], p["key"]


# ================================================================ registry + status

class TestRegistry:
    def test_list_all(self, client):
        rows = client.get("/api/providers").json()
        assert len(rows) == 60

    def test_search_and_filter(self, client):
        assert {r["key"] for r in client.get("/api/providers?category=image").json()} == {
            p["key"] for p in catalog.PROVIDERS if p["category"] == "image"}
        rows = client.get("/api/providers?search=openai").json()
        assert any(r["key"] == "openai" for r in rows)
        rows = client.get("/api/providers?search=minimax").json()  # alias match
        assert any(r["key"] == "miniax" for r in rows)
        rows = client.get("/api/providers?access_class=E").json()
        assert len(rows) == 6
        assert client.get("/api/providers?category=bogus").status_code == 422

    def test_status_honesty(self, client):
        rows = {r["key"]: r for r in client.get("/api/providers").json()}
        # No credentials configured → API providers show need/plan status
        assert rows["openai"]["status"] == "needs_key"
        assert rows["jasper"]["status"] == "plan_required"
        # Non-API tiers never pretend to be integrations
        assert rows["sudowrite"]["status"] == "assisted"
        assert rows["quillbot"]["status"] == "manual"
        assert rows["udio"]["status"] == "external_only"
        assert rows["midjourney"]["status"] == "assisted"
        # Suno MCP is T1 but needs OAuth — no credential fields → configured?
        assert rows["suno"]["tier"] == "T1"

    def test_detail_shape(self, client):
        d = client.get("/api/providers/openai").json()
        assert d["key"] == "openai"
        assert d["shared_credential_with"] == ["openai-images"]
        assert d["credential"]["configured"] is False
        assert {s["method"] for s in d["strategies"]} == {"official_api"}
        assert client.get("/api/providers/nope").status_code == 404


# ================================================================ settings validation

class TestValidation:
    def test_reject_unknown_setting(self):
        assert registry.validate_settings("openai", {"bogus": 1})
        assert registry.validate_settings("openai", {"model": "not-a-model"})
        assert registry.validate_settings("rytr", {"region": "x"})  # no region support

    def test_accept_valid(self):
        assert registry.validate_settings("openai", {
            "model": "gpt-5", "rate_limit_rpm": 60, "timeout_s": 30,
            "max_retries": 2}) == []
        assert registry.validate_settings("qwen-image", {"region": "singapore"}) == []

    def test_config_roundtrip_via_api(self, client):
        r = client.put("/api/providers/openai/config",
                       json={"enabled": True, "settings": {"model": "gpt-5"}})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["enabled"] is True and d["settings"]["model"] == "gpt-5"
        assert client.put("/api/providers/openai/config",
                          json={"settings": {"bogus": 1}}).status_code == 422
        assert client.put("/api/providers/rytr/config",
                          json={"region": "x"}).status_code == 422

    def test_defaults_per_task(self, client):
        r = client.put("/api/providers/defaults",
                       json={"task": "text", "provider_key": "openai"})
        assert r.json()["defaults"]["text"] == "openai"
        # wrong-category default rejected
        assert client.put("/api/providers/defaults",
                          json={"task": "video", "provider_key": "openai"}
                          ).status_code == 422
        assert client.put("/api/providers/defaults",
                          json={"task": "bogus", "provider_key": "x"}
                          ).status_code == 422
        # clear
        client.put("/api/providers/defaults",
                   json={"task": "text", "provider_key": None})
        assert "text" not in client.get("/api/providers/defaults").json()["defaults"]


# ================================================================ credential vault

class TestVault:
    @win_only
    def test_encrypt_roundtrip_isolated(self, tmp_path):
        v = CredentialVault(tmp_path / "c.vault")
        v.set_fields("openai", {"api_key": "sk-secret-123"})
        # roundtrip via a FRESH vault instance (fresh state load)
        v2 = CredentialVault(tmp_path / "c.vault")
        assert v2.get_fields("openai") == {"api_key": "sk-secret-123"}
        st = v2.status("openai")
        assert st["configured"] and st["readable"] and st["fields"] == ["api_key"]

    @win_only
    def test_no_plaintext_on_disk(self, tmp_path):
        v = CredentialVault(tmp_path / "c.vault")
        v.set_fields("anthropic", {"api_key": "sk-ant-zzz-999"})
        raw = (tmp_path / "c.vault").read_text()
        assert "sk-ant-zzz-999" not in raw
        blob = base64.b64decode(json.loads(raw)["providers"]["anthropic"]["fields"]["api_key"])
        assert b"sk-ant" not in blob

    @win_only
    def test_concurrent_writes_do_not_corrupt(self, tmp_path):
        """Two threads racing set_fields on the same vault must leave a
        readable, complete file — no torn JSON, no lost providers."""
        import threading
        v = CredentialVault(tmp_path / "c.vault")
        errs = []

        def put(key, val):
            try:
                for i in range(10):
                    v.set_fields(key, {"api_key": f"{val}-{i}"})
            except Exception as e:  # pragma: no cover
                errs.append(e)

        threads = [threading.Thread(target=put, args=(k, v_))
                   for k, v_ in (("openai", "oai"), ("xai", "x"), ("rytr", "r"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errs
        fresh = CredentialVault(tmp_path / "c.vault")
        for k in ("openai", "xai", "rytr"):
            got = fresh.get_fields(k)
            assert got["api_key"].split("-")[0] in ("oai", "x", "r")

    @win_only
    def test_removal_and_status(self, tmp_path):
        v = CredentialVault(tmp_path / "c.vault")
        v.set_fields("xai", {"api_key": "xai-key"})
        assert v.delete("xai") is True
        assert v.status("xai")["configured"] is False
        assert v.delete("xai") is False

    @win_only
    def test_secret_never_surfaces_via_api(self, client):
        client.put("/api/providers/deepseek/credential",
                   json={"fields": {"api_key": "ds-live-key-42"}})
        detail = client.get("/api/providers/deepseek").text
        assert "ds-live-key-42" not in detail
        assert "ds-live-key-42" not in client.get("/api/providers/deepseek/credential").text
        assert "ds-live-key-42" not in client.get("/api/providers").text

    @win_only
    def test_credential_set_remove_via_api(self, client):
        r = client.put("/api/providers/openai/credential",
                       json={"fields": {"api_key": "sk-test"}})
        assert r.status_code == 200 and r.json()["configured"] is True
        d = client.get("/api/providers/openai").json()
        assert d["credential"]["configured"] is True
        assert d["status"] == "available"  # cred ok but not yet enabled
        # unknown field rejected
        assert client.put("/api/providers/openai/credential",
                          json={"fields": {"bogus": "x"}}).status_code == 422
        r = client.delete("/api/providers/openai/credential")
        assert r.json()["configured"] is False

    @win_only
    def test_status_after_enable(self, client):
        client.put("/api/providers/openai/credential",
                   json={"fields": {"api_key": "sk-test"}})
        client.put("/api/providers/openai/config", json={"enabled": True})
        d = client.get("/api/providers/openai").json()
        assert d["status"] == "configured" and d["enabled"] is True


# ================================================================ mock connection

class TestMockConnection:
    def test_mock_without_credential_fails(self, client):
        r = client.post("/api/providers/openai/test").json()
        assert r["ok"] is False and r["via"] == "mock"
        assert "credential" in r["error"]

    @win_only
    def test_mock_with_credential_passes_but_not_connected(self, client):
        client.put("/api/providers/openai/credential",
                   json={"fields": {"api_key": "sk-test"}})
        r = client.post("/api/providers/openai/test").json()
        assert r["ok"] is True and r["via"] == "mock"
        assert "No live provider contact" in r["note"]
        # Status must NOT flip to "connected" — still just configured-state
        d = client.get("/api/providers/openai").json()
        assert d["status"] != "connected"

    def test_mock_on_assisted_provider(self, client):
        r = client.post("/api/providers/midjourney/test").json()
        # no credential required → mock validates strategy presence
        assert r["via"] == "mock" and r["tier"] == "T2"


# ================================================================ per-project overrides

class TestProjectOverrides:
    def test_project_overlay_read(self, client):
        proj = make_project(client)
        client.put("/api/providers/openai/config",
                   json={"enabled": False, "settings": {"model": "gpt-5"}})
        # project-level override via the command system
        r = client.post(f"/api/projects/{proj['id']}/commands/commit", json={
            "command_type": "project.update_settings",
            "payload": {"settings": {
                "providers": {"openai": {"enabled": True,
                                         "settings": {"model": "gpt-5-mini"}}}}},
            "expected_revision": proj["revision"],
            "idempotency_key": "c1-prov-override-1",
        })
        assert r.status_code == 200, r.text
        rows = client.get(f"/api/projects/{proj['id']}/providers").json()["providers"]
        oai = next(r for r in rows if r["key"] == "openai")
        assert oai["enabled"] is True and oai["overridden"] is True
        assert oai["settings"]["model"] == "gpt-5-mini"
        other = next(r for r in rows if r["key"] == "anthropic")
        assert other["overridden"] is False


# ================================================================ backward compat

class TestBackCompat:
    def test_empty_config_sane_defaults(self, client):
        rows = client.get("/api/providers").json()
        assert all(r["enabled"] is False for r in rows)
        assert client.get("/api/providers/defaults").json()["defaults"] == {}

    def test_existing_settings_untouched(self, client):
        """Provider config writes must not disturb unrelated app_settings rows."""
        from sqlalchemy import select
        from app.db.models import AppSetting
        sf = client.app.state.session_factory
        with sf() as db:
            db.add(AppSetting(key="sentinel.key", value_json={"keep": 1}))
            db.commit()
        client.put("/api/providers/openai/config", json={"enabled": True})
        client.put("/api/providers/defaults",
                   json={"task": "text", "provider_key": "openai"})
        with sf() as db:
            row = db.scalar(select(AppSetting).where(
                AppSetting.key == "sentinel.key"))
            assert row is not None and row.value_json == {"keep": 1}
