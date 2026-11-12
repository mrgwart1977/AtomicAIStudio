"""C1.5 §5 — Desktop CLI detection, phase 1 (detection only).

Covers:
- catalog cli{} metadata: only documented CLIs, validated shape, evidence
- read-only detection: PATH + declared dirs, no subprocess/shell/--version
- statuses: detected / not_found / unsupported_platform
- symlink/reparse rejection; no recursive scanning
- audited user-triggered command; minimized durable audit (no paths)
- no capability-tier promotion; detection is opt-in, never automatic
"""

from __future__ import annotations

import itertools
import os
import stat
import sys
from pathlib import Path

import pytest

from app.db.models import AuditEvent, GenerationJob
from app.domain import cli_detect
from app.domain.providers import catalog, registry
from tests.conftest import make_project

_KEYS = itertools.count()


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, command_type, payload):
    return client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": command_type, "payload": payload,
              "scope": "project", "expected_revision": _rev(client, pid),
              "idempotency_key": f"cli-d-{next(_KEYS):04d}"})


def _sf(client):
    return client.app.state.session_factory


# ---------- catalog metadata ----------

def test_catalog_still_valid():
    assert catalog.validate_catalog() == []


def test_only_documented_clis_have_records():
    have = {p["key"]: p["cli"] for p in catalog.PROVIDERS if p.get("cli")}
    assert set(have) == {"openai", "anthropic", "google-gemini"}
    for key, cli in have.items():
        assert cli["executables"], key
        assert all("/" not in n and "\\" not in n
                   for n in cli["executables"])
        assert set(cli["platforms"]) <= {"windows", "linux", "macos"}
        assert cli["docs_url"].startswith("https://")


def test_cli_metadata_does_not_promote_tiers():
    # Detection metadata must not change any strategy tier/capabilities.
    for p in catalog.PROVIDERS:
        if p.get("cli"):
            assert all(s["tier"] in {"T1", "T2", "T3", "T4"}
                       for s in p["strategies"])


def test_detail_exposes_cli_block(client):
    d = client.get("/api/providers/openai").json()
    assert d["cli"]["executables"] == ["openai"]
    d = client.get("/api/providers/midjourney").json()
    assert d["cli"] is None


# ---------- detection unit behavior ----------

def _prov(exes=("fakecli",), plats=("linux", "windows", "macos"),
          dirs=()):
    return {"key": "t", "cli": {"executables": list(exes),
                                "platforms": list(plats),
                                "install_hint_dirs": list(dirs)}}


def test_detect_not_found(monkeypatch):
    monkeypatch.setattr(cli_detect.shutil, "which", lambda n: None)
    r = cli_detect.detect_cli(_prov(), platform="linux")
    assert r["status"] == "not_found"
    assert r["candidates"] == []


def test_detect_found_via_path(monkeypatch):
    monkeypatch.setattr(cli_detect.shutil, "which",
                        lambda n: "/usr/bin/fakecli" if n == "fakecli" else None)
    monkeypatch.setattr(cli_detect, "_check_candidate", lambda p, platform: "ok")
    r = cli_detect.detect_cli(_prov(), platform="linux")
    assert r["status"] == "detected"
    assert r["candidates"][0]["name"] == "fakecli"
    assert r["candidates"][0]["via"] == "PATH"
    assert r["candidates"][0]["path"].endswith("fakecli")


def test_unsupported_platform():
    r = cli_detect.detect_cli(_prov(plats=("linux",)), platform="windows")
    assert r["status"] == "unsupported_platform"
    assert "windows" in r["reason"]


def test_no_documented_cli():
    r = cli_detect.detect_cli({"key": "t", "cli": None})
    assert r["status"] == "unsupported_platform"


def test_declared_dir_discovery(monkeypatch, tmp_path):
    exe = tmp_path / "fakecli"
    exe.write_bytes(b"#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(cli_detect.shutil, "which", lambda n: None)
    r = cli_detect.detect_cli(
        _prov(dirs=[str(tmp_path)]), platform="linux")
    assert r["status"] == "detected"
    assert r["candidates"][0]["via"] == "install_dir"
    assert r["candidates"][0]["path"].endswith("fakecli")


def test_no_recursive_scan(monkeypatch, tmp_path):
    # Executable buried in a subdirectory must NOT be found.
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "fakecli").write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(cli_detect.shutil, "which", lambda n: None)
    r = cli_detect.detect_cli(
        _prov(dirs=[str(tmp_path)]), platform="linux")
    assert r["status"] == "not_found"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="posix symlink check covered on CI linux")
def test_symlink_rejected(tmp_path, monkeypatch):
    target = tmp_path / "real_cli"
    target.write_bytes(b"#!/bin/sh\n")
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    link = tmp_path / "fakecli"
    link.symlink_to(target)
    monkeypatch.setattr(cli_detect.shutil, "which", lambda n: None)
    r = cli_detect.detect_cli(
        _prov(dirs=[str(tmp_path)]), platform="linux")
    assert r["status"] == "not_found"
    assert r["skipped_untrusted"] == 1


def test_detection_never_executes(monkeypatch, tmp_path):
    # Any attempt to spawn a process during detection must explode the
    # call — proving the code path never reaches exec surfaces.
    def _boom(*a, **kw):
        raise AssertionError("exec attempted during detection")
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", _boom, raising=True)
    monkeypatch.setattr(cli_detect.os, "system", _boom)
    monkeypatch.setattr(cli_detect.shutil, "which",
                        lambda n: str(tmp_path / "fakecli"))
    monkeypatch.setattr(cli_detect, "_check_candidate", lambda p, platform: "ok")
    r = cli_detect.detect_cli(_prov(), platform="linux")
    assert r["status"] == "detected"
    # And the module imports no process-spawning surface at all.
    assert "import subprocess" not in Path(
        cli_detect.__file__).read_text(encoding="utf-8")


# ---------- audited command ----------

def test_detect_is_a_command_not_automatic(client):
    pid = make_project(client)["id"]
    with _sf(client)() as s:
        assert s.query(AuditEvent).filter(
            AuditEvent.payload_json.like("%cli_detect%")).count() == 0
    # detail lookup itself performs no detection
    client.get("/api/providers/openai")
    with _sf(client)() as s:
        assert s.query(AuditEvent).filter(
            AuditEvent.payload_json.like("%cli_detect%")).count() == 0


def test_detect_command_detected(client, monkeypatch):
    pid = make_project(client)["id"]
    monkeypatch.setattr(cli_detect.shutil, "which",
                        lambda n: "C:\\Tools\\openai.exe")
    monkeypatch.setattr(cli_detect, "_check_candidate", lambda p, platform: "ok")
    r = _commit(client, pid, "bridge.cli.detect",
                {"provider_key": "openai"})
    assert r.status_code == 200, r.text
    b = r.json()["bridge"]
    assert b["status"] == "detected"
    assert b["candidates"][0]["path"] == "C:\\Tools\\openai.exe"


def test_detect_command_not_found(client, monkeypatch):
    pid = make_project(client)["id"]
    monkeypatch.setattr(cli_detect.shutil, "which", lambda n: None)
    r = _commit(client, pid, "bridge.cli.detect",
                {"provider_key": "openai"})
    assert r.status_code == 200
    assert r.json()["bridge"]["status"] == "not_found"


def test_detect_requires_documented_cli(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, "bridge.cli.detect",
                {"provider_key": "midjourney"})
    assert r.status_code == 422
    assert "no documented CLI" in r.text


def test_detect_unknown_provider(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, "bridge.cli.detect",
                {"provider_key": "nope"})
    assert r.status_code == 404


def test_audit_minimization_no_paths(client, monkeypatch):
    pid = make_project(client)["id"]
    monkeypatch.setattr(cli_detect.shutil, "which",
                        lambda n: "C:\\Secret\\Path\\openai.exe")
    monkeypatch.setattr(cli_detect, "_check_candidate", lambda p, platform: "ok")
    r = _commit(client, pid, "bridge.cli.detect",
                {"provider_key": "openai"})
    assert r.status_code == 200
    with _sf(client)() as s:
        ev = s.query(AuditEvent).filter(
            AuditEvent.payload_json.like("%cli_detect%")).order_by(
            AuditEvent.id.desc()).first()
    assert ev is not None
    assert "C:\\Secret\\Path" not in ev.payload_json
    assert "openai.exe" not in ev.payload_json


def test_detection_creates_no_jobs_or_assets(client, monkeypatch):
    pid = make_project(client)["id"]
    monkeypatch.setattr(cli_detect.shutil, "which",
                        lambda n: "/usr/bin/openai")
    monkeypatch.setattr(cli_detect, "_check_candidate", lambda p, platform: "ok")
    _commit(client, pid, "bridge.cli.detect", {"provider_key": "openai"})
    with _sf(client)() as s:
        assert s.query(GenerationJob).count() == 0


def test_preview_warns_detection_is_unauthenticated(client):
    pid = make_project(client)["id"]
    r = client.post(f"/api/projects/{pid}/commands/preview",
                    json={"command_type": "bridge.cli.detect",
                          "payload": {"provider_key": "openai"},
                          "scope": "project"})
    assert r.status_code == 200
    assert any("not authenticated" in w.lower()
               for w in r.json()["warnings"])


def test_strategy_resolution_unaffected(client):
    # cli{} presence must not alter strategy resolution (no new dispatch).
    p = catalog.get_provider("openai")
    assert p.get("cli")
    strat, err = __import__(
        "app.domain.bridge", fromlist=["x"]).resolve_strategy(
        p, "launch")
    # openai has no documented launch surface either way — cli{} didn't
    # fabricate one.
    assert strat is None or strat["method"] != "desktop_plugin"


# ---------- executable-resolution hardening (pre-merge review) ----------

def test_directory_named_like_exe_rejected(monkeypatch, tmp_path):
    # A directory named `fakecli` must never count as a detected binary.
    (tmp_path / "fakecli").mkdir()
    monkeypatch.setattr(cli_detect.shutil, "which", lambda n: None)
    r = cli_detect.detect_cli(_prov(dirs=[str(tmp_path)]), platform="linux")
    assert r["status"] == "not_found"


def test_windows_rejects_non_executable_suffix(monkeypatch):
    # A manipulated PATHEXT could resolve `openai.txt` — never accepted.
    monkeypatch.setattr(cli_detect.shutil, "which",
                        lambda n: "C:\Tools\fakecli.txt")
    monkeypatch.setattr(cli_detect, "_has_untrusted_ancestor",
                        lambda p: False)
    monkeypatch.setattr(cli_detect, "_is_untrusted_link", lambda p: False)
    monkeypatch.setattr(cli_detect.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(cli_detect.os, "stat",
                        lambda p, **k: os.stat_result((0o777,) + (0,) * 9))
    r = cli_detect.detect_cli(_prov(), platform="windows")
    assert r["status"] == "not_found"


def test_windows_extensionless_file_not_a_candidate(tmp_path, monkeypatch):
    # Extensionless files can't run on Windows — variants require suffix.
    (tmp_path / "fakecli").write_bytes(b"MZ")
    monkeypatch.setattr(cli_detect.shutil, "which", lambda n: None)
    r = cli_detect.detect_cli(_prov(dirs=[str(tmp_path)]),
                            platform="windows")
    assert r["status"] == "not_found"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="posix ancestor symlink covered on CI linux")
def test_symlinked_ancestor_rejected(tmp_path, monkeypatch):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    exe = real_dir / "fakecli"
    exe.write_bytes(b"#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    link_dir = tmp_path / "linked"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    monkeypatch.setattr(cli_detect.shutil, "which", lambda n: None)
    # Candidate reached through the linked dir is untrusted even though
    # the leaf file itself is real.
    r = cli_detect.detect_cli(_prov(dirs=[str(link_dir)]),
                            platform="linux")
    assert r["status"] == "not_found"
    # And through PATH resolution equally.
    monkeypatch.setattr(cli_detect.shutil, "which",
                        lambda n: str(link_dir / "fakecli"))
    r = cli_detect.detect_cli(_prov(), platform="linux")
    assert r["status"] == "not_found"
    assert r["skipped_untrusted"] >= 1


def test_result_marks_candidates_unverified(monkeypatch):
    monkeypatch.setattr(cli_detect.shutil, "which",
                        lambda n: "/usr/bin/fakecli")
    monkeypatch.setattr(cli_detect, "_check_candidate",
                        lambda p, platform: "ok")
    r = cli_detect.detect_cli(_prov(), platform="linux")
    assert r["verified"] is False
