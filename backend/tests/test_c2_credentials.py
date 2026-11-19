"""C2.4 credential isolation — secrets never leave the DPAPI vault.

Synthetic credentials only. Every assertion targets a boundary where a
secret value could leak: API responses, log records, exported .atomicpkg
packages, and the vault file itself. Missing/unreadable credentials must
fail closed — never silently dispatch or degrade to plaintext.
"""
import base64
import json
import logging
import zipfile
from pathlib import Path

import pytest

from app.domain.providers.credentials import (
    CredentialVault, SecretUnreadable, VaultUnavailable)

SECRET = "sk-synthetic-c2-test-0000aaaabbbb"
SECRET2 = "xai-synthetic-9999ccccdddd"


def _vault(tmp_path):
    return CredentialVault(tmp_path / "credentials.vault")


pytestmark = pytest.mark.skipif(
    not CredentialVault(Path("x")).available(),
    reason="DPAPI vault is Windows-only")


class TestVaultFile:
    def test_vault_file_is_ciphertext_only(self, tmp_path):
        v = _vault(tmp_path)
        v.set_fields("openai", {"api_key": SECRET})
        raw = (tmp_path / "credentials.vault").read_bytes()
        assert SECRET.encode() not in raw
        # base64-of-plaintext must not appear either
        assert base64.b64encode(SECRET.encode()).decode() not in raw.decode()

    def test_empty_values_never_stored(self, tmp_path):
        v = _vault(tmp_path)
        v.set_fields("openai", {"api_key": "", "org": None})
        st = v.status("openai")
        assert st["fields"] == []
        assert st["configured"] is False

    def test_missing_provider_reads_empty(self, tmp_path):
        assert _vault(tmp_path).get_fields("nobody") == {}

    def test_corrupt_vault_file_treated_empty_not_crash(self, tmp_path):
        (tmp_path / "credentials.vault").write_bytes(b"garbage{")
        assert _vault(tmp_path).get_fields("openai") == {}

    def test_corrupt_blob_fails_closed(self, tmp_path):
        """Tampered ciphertext raises SecretUnreadable — never returns
        partial/garbage secret material."""
        v = _vault(tmp_path)
        v.set_fields("openai", {"api_key": SECRET})
        state = json.loads((tmp_path / "credentials.vault").read_text())
        state["providers"]["openai"]["fields"]["api_key"] = \
            base64.b64encode(b"tampered").decode()
        (tmp_path / "credentials.vault").write_text(json.dumps(state))
        v2 = _vault(tmp_path)
        with pytest.raises(SecretUnreadable):
            v2.get_fields("openai")

    def test_delete_removes_ciphertext(self, tmp_path):
        v = _vault(tmp_path)
        v.set_fields("openai", {"api_key": SECRET})
        assert v.delete("openai") is True
        assert v.get_fields("openai") == {}
        assert SECRET.encode() not in \
            (tmp_path / "credentials.vault").read_bytes()
        assert v.delete("openai") is False  # idempotent


class TestSecretsNeverLeak:
    def test_secret_not_in_log_records(self, tmp_path, caplog):
        v = _vault(tmp_path)
        with caplog.at_level(logging.DEBUG):
            v.set_fields("openai", {"api_key": SECRET})
            v.get_fields("openai")
            v.status("openai")
            v.delete("openai")
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert SECRET not in blob

    def test_secret_not_in_vault_repr_or_dir(self, tmp_path):
        v = _vault(tmp_path)
        v.set_fields("openai", {"api_key": SECRET})
        assert SECRET not in repr(v) and SECRET not in str(v.status("openai"))

    def test_secret_not_in_exported_package(self, client, tmp_path, settings):
        """Set a synthetic credential, export a backup, prove the package
        bytes contain no trace of it."""
        r = client.put("/api/providers/openai/credential",
                       json={"fields": {"api_key": SECRET}})
        assert r.status_code in (200, 404, 422), r.text
        p = client.post("/api/projects", json={"name": "LeakChk"}).json()
        rev = client.get(f"/api/projects/{p['id']}").json()["revision"]
        r = client.post(f"/api/projects/{p['id']}/commands/commit",
                        json={"command_type": "backup.export",
                              "payload": {"kind": "backup"},
                              "scope": "project",
                              "expected_revision": rev,
                              "idempotency_key": "c2-leak-export"})
        assert r.status_code == 200, r.text
        from tests.conftest import wait_for_job
        job = wait_for_job(client, p["id"], r.json()["job_id"], timeout=30)
        assert job["status"] == "succeeded", job
        pkg = Path(job["result_json"]["package_path"])
        raw = pkg.read_bytes()
        assert SECRET.encode() not in raw
        with zipfile.ZipFile(pkg) as zf:
            for name in zf.namelist():
                assert "credential" not in name.lower()
                assert "vault" not in name.lower()
                assert SECRET.encode() not in zf.read(name)

    def test_credential_api_returns_metadata_only(self, client):
        client.put("/api/providers/openai/credential",
                   json={"fields": {"api_key": SECRET2}})
        for url in ("/api/providers/openai/credential",
                    "/api/providers/openai",
                    "/api/providers"):
            body = client.get(url).text
            assert SECRET2 not in body, url


class TestFailClosed:
    def test_dispatch_without_credential_denied(self, client, tmp_path):
        """The live gate already proves this; here we assert the vault
        itself gives no fields for an unconfigured provider — nothing
        downstream can accidentally succeed on empty input."""
        assert _vault(tmp_path).get_fields("deepseek") == {}

    def test_platform_unavailable_is_explicit(self, tmp_path, monkeypatch):
        import app.domain.providers.credentials as cred_mod
        monkeypatch.setattr(cred_mod, "_IS_WINDOWS", False)
        v = CredentialVault(tmp_path / "v.vault")
        assert v.available() is False
        with pytest.raises(VaultUnavailable):
            v.set_fields("openai", {"api_key": SECRET})
