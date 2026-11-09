"""Hardware probe shape + migration sanity."""

from __future__ import annotations

from sqlalchemy import inspect

from app.db.session import create_engine_for
from app.main import run_migrations


def test_hardware_probe_shape(client):
    r = client.get("/api/hardware")
    assert r.status_code == 200
    hw = r.json()
    for key in ("os", "cpu", "memory", "gpus", "disks", "runtimes"):
        assert key in hw
    assert isinstance(hw["gpus"], list)
    assert hw["os"]["system"]
    assert hw["memory"]["total_bytes"] > 0
    # GPU list may be empty on CI runners — both shapes are valid.
    for gpu in hw["gpus"]:
        assert "vendor" in gpu and "inference_capable" in gpu


def test_migrations_create_all_tables(settings):
    run_migrations(settings.db_url)
    engine = create_engine_for(settings.db_url)
    tables = set(inspect(engine).get_table_names())
    expected = {
        "projects", "project_revisions", "prompt_drafts", "creative_commands",
        "command_transactions", "generation_jobs", "assets",
        "approved_shot_manifests", "render_cache_entries", "audit_events",
        "alembic_version",
    }
    assert expected <= tables
