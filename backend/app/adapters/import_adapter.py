"""U-2a/U-2b-2a — `ops.model.import`: durable registration of one
user-owned model/adapter file.

mode="reference" (default): the file is inspected (never copied, moved,
or modified) and a `managed_by="external"` artifact row is registered
with support_tier="unknown" — generation-ineligible by construction.

mode="managed_copy" (U-2b-2a, gated by settings.enable_managed_copy):
the file is additionally staged + published into managed storage via
model_copy (os.link no-overwrite, content-verified adoption) and the
artifact is registered managed_by="atomic". Still generation-ineligible
by construction — managed bytes confer zero execution rights.

Privacy contract: the absolute source path lives only in
job.request_json["source_path"] (persisted for execution + crash-requeue;
JobOut never serializes request_json) and the artifact's source_uri
(authenticated detail view only). It never appears in job events,
result_json, audit payloads, error messages, or logs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

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
        # Claim-time gate — a request enqueued while managed_copy was
        # enabled must not proceed after the feature is switched off.
        if (request.get("mode") == "managed_copy"
                and not self._settings.enable_managed_copy):
            return ["mode_not_supported"]
        name = request.get("display_name") or ""
        if len(name) > 200:
            return ["display_name_too_long"]
        if any(ord(ch) < 0x20 for ch in name):
            return ["display_name_invalid"]
        return []

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        req = handle.request
        src = req["source_path"]
        lic = dict(req.get("license") or {})
        mode = req.get("mode") or "reference"

        # Fail-closed execution gate — a job queued while the feature was
        # enabled must still refuse if it was disabled before dispatch.
        if (mode == "managed_copy"
                and not self._settings.enable_managed_copy):
            raise model_import.ImportRefused(
                "mode_not_supported", "managed copying is disabled")

        # Re-validate at execution — the path may have changed since the
        # route's cheap check (enqueue→run gap).
        rp = model_import.validate_source_path(src)
        source_uri = str(rp)
        handle.check_cancel()

        yield {"progress": 0.02, "message": "inspecting file",
               "detail": {"stage": "inspect"}}
        state = {"done": 0, "total": 1, "copy_done": 0, "copy_total": 1}

        def on_bytes(done: int, total: int) -> None:
            state["done"], state["total"] = done, total

        report = model_import.inspect_file(
            rp, cancel_check=handle.check_cancel, progress=on_bytes)
        handle.check_cancel()

        copy_prov: dict[str, Any] | None = None
        if mode == "managed_copy":
            from app.domain import model_copy

            yield {"progress": 0.35, "message": "copying to managed storage",
                   "detail": {"stage": "copy"}}

            def on_copy(done: int, total: int) -> None:
                state["copy_done"], state["copy_total"] = done, total

            # Short read-only session — models_root resolves the app
            # setting for models_dir; registration opens its own below.
            with self._sf() as s:
                copy_prov = model_copy.managed_copy(
                    s, self._settings,
                    job_id=handle.job_id, src_rp=rp, report=report,
                    attempt_no=req.get("job_attempt_no") or 1,
                    cancel_check=handle.check_cancel, progress=on_copy)
            handle.check_cancel()
            yield {"progress": 0.85, "message": "verifying managed copy",
                   "detail": {"stage": "copy",
                              "bytes_done": state["copy_done"],
                              "bytes_total": state["copy_total"]}}

        report_json = report.to_json()
        if copy_prov is not None:
            report_json["copy"] = {
                "dest_rel": copy_prov["dest_rel"],   # basename only
                "expected_sha256": copy_prov["expected_sha256"],
                "verified_sha256": copy_prov["verified_sha256"],
                "bytes_written": copy_prov["bytes_written"],
                "provenance": copy_prov["provenance"],
                "published_at": copy_prov["published_at"],
                "staging_left": copy_prov["staging_left"],
            }

        yield {"progress": 0.9, "message": "registering artifact",
               "detail": {"stage": "register"}}
        with self._sf() as s:
            try:
                row, reused = model_artifacts.register_import(
                    s, source_uri=source_uri, report=report_json,
                    license_json=lic,
                    family=req.get("family") or "",
                    role_hint=req.get("role_hint") or "",
                    display_name=req.get("display_name") or "",
                    managed_uri=copy_prov["dest_abs"]
                    if copy_prov is not None else None)
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
                        "managed_by": row.managed_by,
                    }))
                s.commit()
                artifact_id = row.artifact_id
            except SQLAlchemyError:
                # F1 — a DB failure string carries statement params,
                # which include the source path. Roll back and surface
                # only a sanitized code: the raw error must never reach
                # job.error, result_json, events, responses, or logs.
                try:
                    s.rollback()
                except SQLAlchemyError:
                    pass  # rollback on a dead connection — nothing to lose
                raise model_import.ImportRefused(
                    "db_error",
                    "artifact registration failed") from None

        result: dict[str, Any] = {
            "artifact_id": artifact_id,
            "status": "registered" if not reused else "reused",
            "report": report_json,
        }
        if copy_prov is not None:
            result["managed"] = {
                "provenance": copy_prov["provenance"],
                "dest_rel": copy_prov["dest_rel"],
            }
        yield {
            "done": True, "progress": 1.0,
            "output": {"no_asset": True, "result": result},
        }
