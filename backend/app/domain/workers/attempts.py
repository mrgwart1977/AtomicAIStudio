"""Durable execution-attempt records (M4.1 Phase 1).

Every job execution attempt gets a stable ID recorded on the durable
job row BEFORE work begins — reconciliation can always ask "what was
in flight when we died?". Phase 1 stores attempts inside
``generation_jobs.progress_json["attempts"]`` — a JSON field, no schema
change. The proposed `0008` migration (separate approval) promotes them
to a first-class ``execution_attempts`` table + ``worker_runs`` table;
see docs/plans/M4_1_PERSISTENT_WORKERS.md.

Outcomes: running → succeeded | failed | cancelled | worker_dead |
interrupted (restart found it mid-flight).
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, attributes as orm_attributes

from app.db.models import GenerationJob

ATTEMPTS_KEY = "attempts"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attempts_copy(job: GenerationJob) -> dict:
    """Deep-copied progress_json. Nested lists/dicts must not be shared —
    an in-place mutation of a shared structure is invisible to the ORM
    flush and silently fails to persist."""
    return copy.deepcopy(job.progress_json or {})


def _persist(session: Session, job: GenerationJob, pj: dict) -> None:
    job.progress_json = pj
    orm_attributes.flag_modified(job, "progress_json")
    session.commit()


def start_attempt(session: Session, job: GenerationJob,
                  worker_id: str | None = None) -> dict[str, Any]:
    """Append a new attempt record to the job and return it. Caller
    commits. Attempt IDs are uuid4 — stable and unique per execution."""
    pj = _attempts_copy(job)
    rec = {
        "attempt_id": uuid.uuid4().hex[:16],
        "attempt_no": job.attempt_no or 1,
        "worker_id": worker_id,
        "started_at": _now(),
        "ended_at": None,
        "outcome": "running",
    }
    pj.setdefault(ATTEMPTS_KEY, []).append(rec)
    _persist(session, job, pj)
    return rec


def _find(pj: dict, attempt_id: str) -> dict | None:
    for rec in pj.get(ATTEMPTS_KEY) or []:
        if rec.get("attempt_id") == attempt_id:
            return rec
    return None


def patch_attempt(session: Session, job: GenerationJob, attempt_id: str,
                  patch: dict[str, Any]) -> None:
    """Merge fields (e.g. worker_id assigned later) into an attempt."""
    pj = _attempts_copy(job)
    rec = _find(pj, attempt_id)
    if rec is not None:
        rec.update(patch)
        _persist(session, job, pj)


def finish_attempt(session: Session, job: GenerationJob, attempt_id: str,
                   outcome: str) -> None:
    pj = _attempts_copy(job)
    rec = _find(pj, attempt_id)
    if rec is not None:
        rec["outcome"] = outcome
        rec["ended_at"] = _now()
        _persist(session, job, pj)


def list_attempts(job: GenerationJob) -> list[dict[str, Any]]:
    return list((job.progress_json or {}).get(ATTEMPTS_KEY) or [])


def running_attempts(job: GenerationJob) -> list[dict[str, Any]]:
    return [a for a in list_attempts(job) if a.get("outcome") == "running"]
