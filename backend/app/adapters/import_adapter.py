"""U-2a — `ops.model.import`: durable registration of one user-owned
model/adapter file. Reference-only: the file is inspected (never copied,
moved, or modified) and a `managed_by="external"` artifact row is
registered with support_tier="unknown" — generation-ineligible by
construction.

Privacy contract: the absolute source path lives only in
job.request_json["source_path"] (persisted for execution + crash-requeue;
JobOut never serializes request_json) and the artifact's source_uri
(authenticated detail view only). It never appears in job events,
result_json, audit payloads, error messages, or logs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.adapters.base import CapabilityProfile, JobHandle
from app.domain import model_artifacts, model_import


class ModelImportAdapter:
    profile = CapabilityProfile(
        adapter_id="ops.model.import",
        display_name="Import model file",
        adapter_kind="local",
        tasks=("model_import",),
        supports={"verify": True},
        limits={},
        requires_cloud=False,
        cost_model="free",
        lane="ops",
        needs_gpu=False,
        # Idempotent by construction (full re-hash + verified reuse), so
        # an interrupted import safely re-queues on restart.
        resumable=True,
        notes="User-approved reference import: inspect + hash + register, "
              "no file writes, no execution eligibility.",
    )

    def __init__(self, session_factory, settings) -> None:
        self._sf = session_factory
        self._settings = settings

    async def validate(self, request: dict[str, Any]) -> list[str]:
        """Enqueue-time gate. Error strings are taxonomy codes — never
        raw paths (they land in job.error verbatim)."""
        src = request.get("source_path")
        if not src or not isinstance(src, str):
            return ["path_not_absolute"]
        try:
            model_import.validate_source_path(src)
        except model_import.ImportRefused as e:
            return [e.code]
        lic = request.get("license") or {}
        if not (lic.get("name") or "").strip():
            return ["license_required"]
        return []

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        req = handle.request
        src = req["source_path"]
        lic = dict(req.get("license") or {})

        # Re-validate at execution — the path may have changed since the
        # route's cheap check (enqueue→run gap).
        rp = model_import.validate_source_path(src)
        source_uri = str(rp)
        handle.check_cancel()

        yield {"progress": 0.02, "message": "inspecting file"}
        state = {"done": 0, "total": 1}

        def on_bytes(done: int, total: int) -> None:
            state["done"], state["total"] = done, total

        report = model_import.inspect_file(
            rp, cancel_check=handle.check_cancel, progress=on_bytes)
        handle.check_cancel()

        yield {"progress": 0.9, "message": "registering artifact"}
        with self._sf() as s:
            row, reused = model_artifacts.register_import(
                s, source_uri=source_uri, report=report.to_json(),
                license_json=lic,
                family=req.get("family") or "",
                role_hint=req.get("role_hint") or "")
            # Audit without the path — artifact_id is the durable ref.
            from app.db.models import AuditEvent, new_id
            s.add(AuditEvent(
                id=new_id(), project_id=None,
                event_type="model_import.registered",
                payload_json={
                    "artifact_id": row.artifact_id,
                    "reused": reused,
                    "fmt": report.fmt,
                    "suggested_arch": report.suggested_arch,
                }))
            s.commit()
            artifact_id = row.artifact_id

        yield {
            "done": True, "progress": 1.0,
            "output": {"no_asset": True, "result": {
                "artifact_id": artifact_id,
                "status": "registered" if not reused else "reused",
                "report": report.to_json(),
            }},
        }
