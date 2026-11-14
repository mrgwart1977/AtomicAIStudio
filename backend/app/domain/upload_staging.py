"""S2 — staged-upload lifecycle for the C3-D4 upload-source area.

`POST /assets/upload-source` writes content-named files under
``data_dir/uploads/{project_id}/{sha256}.{format}`` so the ordinary
``asset.import`` durable job can ingest them through CAS. Without a
retention policy those staging files could accumulate forever; this
module is the conservative sweeper.

Policy
------
- A staged file younger than :data:`RETENTION` is never deleted — it
  may be minutes away from its import commit.
- An older file is kept if any *recoverable* job (``queued`` or
  ``running``) still references it as ``source_path`` — deleting it
  would break restart recovery.
- Everything else is unlinked individually (no recursive deletion,
  never leaving the project staging dir) and its byte size tallied.
- The sweeper never touches CAS blobs — staged files live only under
  ``uploads/`` and CAS assets are content-addressed elsewhere.
- Deletion is best-effort: a file locked by a concurrent in-flight
  import raises OSError and is simply kept this round.

Reports carry counts and reclaimed bytes only — staged paths are
server-owned and never surfaced in API responses.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import GenerationJob

log = logging.getLogger(__name__)

# Conservative window: staged bytes are kept a full day so a crashed or
# simply slow import commit always finds its source.
RETENTION = timedelta(hours=24)

# Jobs in these states survive a backend restart (queued work is
# re-armed; running work is reconciled to queued). Their referenced
# staging files are protected.
RECOVERABLE = ("queued", "running")


def uploads_dir(data_dir: Path, project_id: str) -> Path:
    return Path(data_dir) / "uploads" / project_id


def stage_path(data_dir: Path, project_id: str, sha256: str, fmt: str) -> Path:
    return uploads_dir(data_dir, project_id) / f"{sha256}.{fmt}"


def referenced_paths(db: Session, project_id: str) -> set[str]:
    """``source_path`` values of recoverable jobs in this project."""
    out: set[str] = set()
    rows = db.execute(
        select(GenerationJob).where(
            GenerationJob.project_id == project_id,
            GenerationJob.status.in_(RECOVERABLE),
        )
    ).scalars()
    for job in rows:
        req = job.request_json or {}
        sp = req.get("payload", {}).get("source_path") or req.get("source_path")
        if sp:
            out.add(str(sp))
    return out


def cleanup_project_staged(
    db: Session,
    data_dir: Path,
    project_id: str,
    *,
    now: float | None = None,
    retention: timedelta = RETENTION,
) -> dict:
    """Sweep one project's staging dir. Returns counts only."""
    now = time.time() if now is None else now
    up = uploads_dir(data_dir, project_id)
    kept = deleted = reclaimed = 0
    if not up.is_dir():
        return {"kept": 0, "deleted": 0, "reclaimed_bytes": 0}
    refs = referenced_paths(db, project_id)
    cutoff = now - retention.total_seconds()
    for entry in up.iterdir():          # flat — never recurse
        # Symlinks are never followed or unlinked — a planted link can
        # neither redirect deletion outside staging nor be cleaned
        # itself; foreign entries (dirs, junctions) are also skipped.
        if entry.is_symlink() or not entry.is_file():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff or str(entry) in refs:
            kept += 1
            continue
        try:
            entry.unlink()
        except OSError:
            kept += 1                      # locked by a live import — keep
            continue
        deleted += 1
        reclaimed += st.st_size
    return {"kept": kept, "deleted": deleted, "reclaimed_bytes": reclaimed}


def sweep_all_staged(session_factory, data_dir: Path) -> dict:
    """Startup sweep across every project's staging dir. Called during
    lifespan after job reconciliation so recoverable references are
    already visible."""
    base = Path(data_dir) / "uploads"
    total = {"kept": 0, "deleted": 0, "reclaimed_bytes": 0}
    if not base.is_dir():
        return total
    with session_factory() as db:
        for proj_dir in base.iterdir():
            if not proj_dir.is_dir():
                continue
            r = cleanup_project_staged(db, data_dir, proj_dir.name)
            for k in total:
                total[k] += r[k]
    if total["deleted"]:
        log.info("staged-upload sweep: %d expired files, %d bytes reclaimed",
                 total["deleted"], total["reclaimed_bytes"])
    return total
