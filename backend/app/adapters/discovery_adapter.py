"""U-3 — `ops.model.discover`: bounded enumeration of user-approved
roots into unverified candidate rows.

Two phases, strictly ordered:
  A — scan: each enabled root is walked read-only (no DB writes during
      the walk); candidates + the root outcome commit in ONE
      transaction per root. A mid-root cancel/crash discards that
      root's uncommitted observations entirely.
  C — reconcile: ONLY on the normal-completion path (after every root
      commit), a single all-or-nothing transaction marks `absent`
      purely on complete-scan coverage. Cancel at the gate or
      mid-transaction rolls back — a cancelled/interrupted scan can
      never create an absent state.

Privacy: progress/detail carry counts and root_ids only — never
paths. Result_json is path-free by construction.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.adapters.base import CapabilityProfile, JobCancelled, JobHandle
from app.db.models import utcnow
from app.domain import model_discovery


class ModelDiscoveryAdapter:
    profile = CapabilityProfile(
        adapter_id="ops.model.discover",
        display_name="Discover model files",
        adapter_kind="local",
        tasks=("model_discovery",),
        supports={"verify": True},
        limits={
            "max_depth": model_discovery.MAX_DEPTH_CAP,
            "max_entries_per_root": model_discovery.MAX_ENTRIES_PER_ROOT,
            "max_candidates_per_root":
                model_discovery.MAX_CANDIDATES_PER_ROOT,
            "time_budget_s": model_discovery.SCAN_TIME_BUDGET_S,
        },
        requires_cloud=False,
        cost_model="free",
        lane="ops",
        needs_gpu=False,
        # Upserts are keyed on deterministic ids, so a requeued job
        # re-enumerates safely (F1 idempotent observations).
        resumable=True,
        notes="Read-only enumeration of user-approved folders into "
              "unverified candidate rows; import remains a separate "
              "user-approved U-2 job.",
    )

    def __init__(self, session_factory, settings) -> None:
        self._sf = session_factory
        self._settings = settings

    async def validate(self, request: dict[str, Any]) -> list[str]:
        return []

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        job_id = handle.job_id
        deadline = (time.monotonic()
                    + model_discovery.SCAN_TIME_BUDGET_S)
        yield {"progress": 0.02, "message": "scanning approved folders",
               "detail": {"stage": "scan"}}

        # Snapshot the enabled root ids once; per-root state is
        # re-read inside each commit transaction. Managed app-owned
        # storage (imports/ + .staging/) is excluded from every walk —
        # it is never "discovered user files".
        with self._sf() as s:
            root_ids = [r.root_id for r in model_discovery.enabled_roots(s)]
            exclude = model_discovery.managed_exclusions(
                s, self._settings)
        total = len(root_ids)
        done = 0
        entries = 0
        found = 0
        outcomes: dict[str, str] = {}

        # -------- Phase A — per-root bounded walk + atomic commit ----
        for i, rid in enumerate(root_ids):
            handle.check_cancel()
            with self._sf() as s:
                root = s.get(model_discovery.DiscoveryRoot, rid)
                if root is None or not root.enabled \
                        or root.archived_at is not None:
                    continue   # archived/disabled mid-scan — not visited
                try:
                    scan = model_discovery.scan_root_fs(
                        root, cancel_check=handle.check_cancel,
                        deadline=deadline, exclude=exclude)
                    found += model_discovery.commit_root_scan(
                        s, root, job_id, scan, utcnow())
                    s.commit()
                except JobCancelled:
                    s.rollback()
                    raise
                except SQLAlchemyError:
                    # A DB failure writes nothing for this root — mark
                    # the outcome honestly so it can never supply
                    # absence evidence.
                    s.rollback()
                    try:
                        model_discovery.record_unscanned_roots(
                            s, job_id, [rid], "refused", "db_error",
                            utcnow())
                        s.commit()
                    except SQLAlchemyError:
                        s.rollback()
                    scan = {"outcome": "refused", "code": "db_error",
                            "entries": 0, "candidates": []}
                outcomes[rid] = scan["outcome"]
                entries += scan["entries"]
                done += 1
                yield {"progress": 0.05 + 0.85 * (i + 1) / max(total, 1),
                       "message": "scanning approved folders",
                       "detail": {"stage": "scan",
                                  "roots_done": done,
                                  "roots_total": total,
                                  "candidates_found": found}}

        # Roots never reached (time budget) get an honest outcome.
        unreached = [r for r in root_ids if r not in outcomes]
        if unreached:
            with self._sf() as s:
                try:
                    model_discovery.record_unscanned_roots(
                        s, job_id, unreached, "truncated",
                        "time_budget", utcnow())
                    s.commit()
                except SQLAlchemyError:
                    s.rollback()
            for rid in unreached:
                outcomes[rid] = "truncated"

        # -------- Phase C — all-or-nothing absence reconciliation ---
        handle.check_cancel()          # FINAL GATE — cancel before
        recon = "done"                 #   reconcile never marks absent
        marked = 0
        with self._sf() as s:
            try:
                marked = model_discovery.reconcile_absent(
                    s, job_id, cancel_check=handle.check_cancel,
                    ts=utcnow())
                s.commit()
            except JobCancelled:
                s.rollback()           # uncommitted absent marks die
                raise                  # job → cancelled, zero absent
            except SQLAlchemyError:
                s.rollback()
                recon = "failed"       # positive data intact; job
                                       # still completes honestly

        yield {
            "done": True, "progress": 1.0,
            "output": {"no_asset": True, "result": {
                "roots_done": done, "roots_total": total,
                "entries_seen": entries, "candidates_found": found,
                "root_outcomes": outcomes,
                "absent_reconcile": recon,
                "absent_marked": marked if recon == "done" else 0,
            }},
        }
