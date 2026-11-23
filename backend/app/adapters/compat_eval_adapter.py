"""U-4b — `ops.model.compat_eval`: the ONLY writer of compat_verdicts.

The serialized ops lane is the single write path for verdict mutations —
there is no synchronous API write anywhere. scope="artifact" evaluates
one registered artifact; scope="all" snapshots every artifact_id once
and evaluates each in its own commit (per-artifact atomicity — a
mid-run cancel can never leave a half-written artifact).

Deterministic by construction:
  - verdicts are pure functions of stored evidence + live state;
  - upserts replace the full verdict set for an artifact in one
    transaction;
  - a resume re-enters the same loop and skips artifacts whose stored
    fingerprint already matches the live inputs (no-op writes);
  - an artifact deleted mid-job is skipped as artifact_gone — never a
    crash, never a partial row.

Privacy: progress/result carry artifact_ids and codes only — never
paths, source_uri, or provider data.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.adapters.base import CapabilityProfile, JobCancelled, JobHandle
from app.db.models import CompatVerdict, ModelArtifact, new_id, utcnow
from app.domain import compat_verdicts, model_families


class CompatEvalError(Exception):
    """Sanitized failure — `code` is the entire error payload (never a
    path or raw exception string; it lands in job.error verbatim)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _verdict_rows(v_list, *, job_id: str, hw: dict,
                  cls, decls: dict, rstates: dict,
                  evaluated_at) -> list[CompatVerdict]:
    rows = []
    for v in v_list:
        decl = decls[v.runtime_key]
        rows.append(CompatVerdict(
            id=new_id(),
            artifact_id=v.artifact_id,
            runtime_key=v.runtime_key,
            arch_axis=v.arch_axis,
            arch_confidence=v.arch_confidence,
            runtime_axis=v.runtime_axis,
            hardware_axis=v.hardware_axis,
            policy_axis=v.policy_axis,
            level=v.level,
            domain_levels_json=dict(v.domain_levels),
            overall=v.overall,
            blocking_axes_json=list(v.blocking_axes),
            confidence=v.confidence,
            reasons_json=list(v.reasons),
            warnings_json=list(v.warnings),
            missing_json=list(v.missing),
            hardware_limits_json=list(v.hardware_limits),
            actions_json=list(v.actions),
            evidence_json=compat_verdicts.evidence_payload(
                decl, rstates[v.runtime_key], hw, cls, evaluated_at),
            inputs_fingerprint=v.inputs_fingerprint,
            ruleset_version=v.ruleset_version,
            job_id=job_id,
            evaluated_at=evaluated_at,
        ))
    return rows


class ModelCompatEvalAdapter:
    profile = CapabilityProfile(
        adapter_id="ops.model.compat_eval",
        display_name="Evaluate model compatibility",
        adapter_kind="local",
        tasks=("model_compat_eval",),
        supports={"verify": True},
        limits={},
        requires_cloud=False,
        cost_model="free",
        lane="ops",
        needs_gpu=False,
        # Idempotent: resume re-derives verdicts and skips artifacts whose
        # stored fingerprint already matches live inputs.
        resumable=True,
        notes="Stored-evidence compatibility evaluation — the only "
              "compat_verdicts writer; never inspects model files, "
              "executes runtimes, or changes inference eligibility.",
    )

    def __init__(self, session_factory, settings, hw_probe=None) -> None:
        self._sf = session_factory
        self._settings = settings
        self._hw_probe = hw_probe  # tests inject; None → live probe

    async def validate(self, request: dict[str, Any]) -> list[str]:
        scope = request.get("scope")
        if scope not in ("artifact", "all"):
            return ["bad_scope"]
        if scope == "artifact" and not request.get("artifact_id"):
            return ["artifact_id_required"]
        return []

    def _probe(self) -> dict:
        if self._hw_probe is not None:
            return self._hw_probe()
        from app.domain import hardware
        return hardware.probe()

    def _eval_one(self, s, row: ModelArtifact, hw: dict,
                  gpu_prefs: dict, job_id: str) -> tuple[int, list[str]]:
        """Evaluate + upsert ONE artifact inside its own transaction.
        Returns (rows_written, fingerprints). Caller commits/rolls back."""
        view = model_families.view_from_row(row)
        cls = model_families.classify_artifact(view)
        rstates = compat_verdicts.runtime_states_by_decl(s)
        decls = compat_verdicts.RUNTIME_CAPS
        now = utcnow()
        v_list = compat_verdicts.evaluate_artifact(
            view, cls,
            content_sha256=row.content_sha256,
            license_json=row.license_json,
            runtime_states=rstates, hw=hw, gpu_prefs=gpu_prefs)

        existing = s.execute(
            select(CompatVerdict).where(
                CompatVerdict.artifact_id == row.artifact_id)
        ).scalars().all()
        old = sorted((r.runtime_key, r.inputs_fingerprint)
                     for r in existing)
        new = sorted((v.runtime_key, v.inputs_fingerprint)
                     for v in v_list)
        if old == new and len(existing) == len(v_list):
            return 0, [fp for _, fp in new]      # resume no-op

        for r in existing:
            s.delete(r)
        for r in _verdict_rows(v_list, job_id=job_id, hw=hw, cls=cls,
                               decls=decls, rstates=rstates,
                               evaluated_at=now):
            s.add(r)
        return len(v_list), [fp for _, fp in new]

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        req = handle.request
        scope = req.get("scope")
        if scope not in ("artifact", "all"):
            raise CompatEvalError("bad_scope")

        yield {"progress": 0.02, "message": "evaluating compatibility",
               "detail": {"stage": "snapshot"}}
        hw = self._probe()
        with self._sf() as s:
            gpu_prefs = compat_verdicts.gpu_prefs(s)
            if scope == "artifact":
                ids = [req["artifact_id"]]
            else:
                ids = [r.artifact_id for r in s.execute(
                    select(ModelArtifact)
                    .order_by(ModelArtifact.artifact_id)).scalars()]

        total = len(ids)
        done = evaluated = skipped = gone = 0
        errors: list[dict] = []
        t0 = time.monotonic()

        for aid in ids:
            handle.check_cancel()                 # per-artifact gate
            try:
                with self._sf() as s:
                    row = s.get(ModelArtifact, aid)
                    if row is None:
                        gone += 1
                        errors.append({"artifact_id": aid,
                                       "code": "artifact_gone"})
                        continue
                    wrote, _ = self._eval_one(
                        s, row, hw, gpu_prefs, handle.job_id)
                    evaluated += 1 if wrote else 0
                    skipped += 0 if wrote else 1
                    s.commit()
            except JobCancelled:
                raise
            except SQLAlchemyError:
                errors.append({"artifact_id": aid, "code": "db_error"})
                if scope == "artifact":
                    raise CompatEvalError("db_error") from None
            except Exception:  # noqa: BLE001 — sanitized by contract
                # Raw exception strings can carry absolute paths; only
                # the code crosses into the durable record.
                errors.append({"artifact_id": aid, "code": "eval_error"})
                if scope == "artifact":
                    raise CompatEvalError("eval_error") from None
            finally:
                done += 1
            yield {"progress": 0.05 + 0.9 * done / max(total, 1),
                   "message": "evaluating compatibility",
                   "detail": {"stage": "evaluate", "done": done,
                              "total": total}}

        if scope == "artifact" and gone:
            raise CompatEvalError("artifact_gone")

        yield {
            "done": True, "progress": 1.0,
            "output": {"no_asset": True, "result": {
                "scope": scope,
                "artifacts": total,
                "evaluated": evaluated,
                "skipped_current": skipped,
                "artifact_gone": gone,
                "errors": errors,
                "elapsed_s": round(time.monotonic() - t0, 3),
            }},
        }
