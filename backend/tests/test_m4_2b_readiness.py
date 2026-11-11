"""M4.1 readiness — catalog routing, model↔adapter compatibility, and the
generate.image.persistent command path. NO inference, NO weights, NO real
runtime anywhere — catalog checks are static metadata, command tests end at
the durable job row (the job then fails honestly with no adapter/binary).

Verified-against-runtime notes: the request payload is pinned to the
/sdapi/v1/txt2img documented field set (api.md @ master-1073-991f825);
the 503 model_not_loaded path is covered in test_m4_2b.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain import model_catalog
from app.domain.commands import (
    GENERATION_COMMANDS,
    JOB_COMMANDS,
    SUPPORTED_COMMANDS,
)
from tests.conftest import make_project, wait_for_job
from tests.test_m3 import _commit, _install_row

PERSISTENT = "gen.image.sdcpp-server"


# ================================================================ catalog routing

class TestCatalogRouting:
    def test_sd15_entries_declare_persistent_compat(self):
        """The validated SD 1.5 family opts into the persistent adapter —
        primary adapter_id stays gen.image.sdcpp (existing installs and
        the direct adapter are untouched)."""
        for key in ("sd15.fp16", "sd15.q8", "sd15.fp32"):
            spec = model_catalog.get(key)
            assert spec.adapter_id == "gen.image.sdcpp"
            assert PERSISTENT in spec.compatible_adapters
            assert model_catalog.serves_adapter(spec, PERSISTENT)
            assert model_catalog.serves_adapter(spec, "gen.image.sdcpp")

    def test_unverified_models_do_not_serve_persistent(self):
        """Models whose contract is unverified on the persistent path
        (SDXL/FLUX — heavier, untested residency) are excluded honestly."""
        for key in ("sdxl.base", "sdxl.turbo", "flux.schnell", "flux.dev"):
            spec = model_catalog.get(key)
            assert not model_catalog.serves_adapter(spec, PERSISTENT), key

    def test_serves_adapter_primary_always_true(self):
        for spec in model_catalog.catalog():
            assert model_catalog.serves_adapter(spec, spec.adapter_id)

    def test_spec_to_dict_exposes_compatible_adapters(self):
        out = model_catalog.spec_to_dict(model_catalog.get("sd15.fp16"))
        assert out["adapter_id"] == "gen.image.sdcpp"
        assert out["compatible_adapters"] == [PERSISTENT]


# ================================================================ command routing

class TestCommandRouting:
    def test_persistent_command_registered_everywhere(self):
        assert "generate.image.persistent" in SUPPORTED_COMMANDS
        assert "generate.image.persistent" in GENERATION_COMMANDS
        assert JOB_COMMANDS["generate.image.persistent"] == PERSISTENT
        # The direct path is untouched.
        assert JOB_COMMANDS["generate.image"] == "gen.image.sdcpp"

    def test_unknown_model_rejected(self, client, tmp_path):
        p = make_project(client)
        r = _commit(client, p["id"], "generate.image.persistent",
                    {"model_key": "nope", "prompt": "a cat"}, "pers-unknown", expect=422)
        assert "unknown model" in r.json()["detail"]

    def test_not_installed_rejected(self, client, tmp_path):
        p = make_project(client)
        r = _commit(client, p["id"], "generate.image.persistent",
                    {"model_key": "sd15.fp16", "prompt": "a cat"},
                    "pers-notinst", expect=422)
        assert "not installed" in r.json()["detail"]

    def test_incompatible_model_rejected(self, client, tmp_path):
        """Installed + licensed + image-capable — but NOT declared
        compatible with the persistent adapter → honest 422."""
        _install_row(client, "sdxl.base", tmp_path)
        p = make_project(client)
        r = _commit(client, p["id"], "generate.image.persistent",
                    {"model_key": "sdxl.base", "prompt": "a cat"},
                    "pers-incompat", expect=422)
        assert "not declared compatible" in r.json()["detail"]

    def test_compatible_model_enqueues_persistent_job(self, client, tmp_path):
        """Full path: validation → durable job row on the persistent
        adapter. The job then fails honestly (no adapter without the
        feature gate, no runtime binary anyway) — which is the correct
        readiness-state behavior."""
        _install_row(client, "sd15.fp16", tmp_path)
        p = make_project(client)
        out = _commit(client, p["id"], "generate.image.persistent",
                      {"model_key": "sd15.fp16", "prompt": "a lighthouse",
                       "width": 512, "height": 512, "seed": 7},
                      "pers-enqueue")
        job_id = out["job_id"]
        jobs = client.get(f"/api/projects/{p['id']}/jobs").json()
        job = next(j for j in jobs if j["id"] == job_id)
        assert job["adapter_id"] == PERSISTENT
        assert job["model_key"] == "sd15.fp16"
        final = wait_for_job(client, p["id"], job_id)
        assert final["status"] == "failed"
        assert final["error"]  # honest failure, not silent success

    def test_preview_warns_experimental(self, client, tmp_path):
        """Preview surfaces the experimental-worker warning honestly."""
        _install_row(client, "sd15.fp16", tmp_path)
        p = make_project(client)
        rev = client.get(f"/api/projects/{p['id']}").json()["revision"]
        r = client.post(
            f"/api/projects/{p['id']}/commands/preview",
            json={"command_type": "generate.image.persistent",
                  "payload": {"model_key": "sd15.fp16", "prompt": "x"},
                  "scope": "project", "origin": "test",
                  "expected_revision": rev,
                  "idempotency_key": "pers-prev-0001"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["launches_generation"] is True
        assert any("persistent" in w.lower() for w in body["warnings"])
