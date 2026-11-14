from __future__ import annotations

import collections
import sys
import time
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

TOKEN = "test-token"

# Captured BEFORE any fixture patching — lets the real-hardware
# integration path (test_m4_safety) restore the genuine psutil probe.
REAL_VIRTUAL_MEMORY = psutil.virtual_memory

_SVMEM = collections.namedtuple(
    "svmem", ["total", "available", "percent", "used", "free"])
_GIB = 1024 ** 3
_ROOMY_SVMEM = _SVMEM(
    total=64 * _GIB, available=48 * _GIB, percent=25.0,
    used=16 * _GIB, free=48 * _GIB)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(session_token=TOKEN, data_dir=tmp_path / "data", log_level="WARNING")


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        c.headers["X-Atomic-Token"] = TOKEN
        yield c


@pytest.fixture()
def authed(client):
    return {"X-Atomic-Token": TOKEN}


def make_project(client, name="Test Project", template="blank") -> dict:
    r = client.post("/api/projects", json={"name": name, "template": template})
    assert r.status_code == 201, r.text
    return r.json()


def wait_for_job(client, project_id: str, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = client.get(f"/api/projects/{project_id}/jobs").json()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job and job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.15)
    raise TimeoutError(f"job {job_id} did not finish within {timeout}s")


@pytest.fixture(autouse=True)
def _roomy_memory(monkeypatch):
    """Deterministic memory headroom for every test: the safety preflight
    measures the REAL host — on a loaded 16 GB dev box that would make
    gen-job tests flaky. Tests that exercise the gate itself override
    this with their own monkeypatch.

    Two admission sources exist and BOTH are pinned here:
    - ``resources.system_memory`` — the command/job preflight gate.
    - ``psutil.virtual_memory`` — the WorkerPool resident-worker RAM
      probe (main.py wires ``ram_free_bytes=lambda:
      psutil.virtual_memory().available``), plus firstrun_watchdog.
    """
    from app.domain import resources
    monkeypatch.setattr(resources, "system_memory", lambda: {
        "total_phys_bytes": 64 * resources.GIB, "avail_phys_bytes": 48 * resources.GIB,
        "phys_load_pct": 25, "commit_limit_bytes": 128 * resources.GIB,
        "commit_charge_bytes": 16 * resources.GIB,
        "commit_headroom_bytes": 112 * resources.GIB})
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _ROOMY_SVMEM)
