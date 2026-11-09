from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

TOKEN = "test-token"


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
