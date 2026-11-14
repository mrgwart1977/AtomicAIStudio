"""Application entry point.

Run standalone for development:
    python -m app.main --port 8788 --token dev --data-dir ./data

The Tauri shell launches the same module as a managed sidecar with a
generated token and an ephemeral port; it also stops this process when
the desktop app exits.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.registry import AdapterRegistry
from app.api.providers_router import providers_router
from app.api.routers import api_router, open_router, websocket_endpoint
from app.config import Settings, ensure_dirs, get_settings
from app.db.session import create_engine_for, session_factory
from app.domain.events import EventBus
from app.domain.instance_lock import acquire_data_dir_lock
from app.domain.jobs import JobEngine
from app.domain.watch_service import ImportWatchService
from app.logging_config import configure_logging
from app.security import require_token

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _alembic_script_location() -> Path:
    """Migration scripts live in the source tree during development and are
    bundled under backend_res/alembic inside the PyInstaller onedir."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "backend_res" / "alembic"
    return BACKEND_DIR / "app" / "db" / "alembic"


class UnsupportedRevisionError(RuntimeError):
    """The database was stamped by an app build that knows revisions
    this build does not — almost always a NEWER version."""


def _assert_supported_revision(cfg: AlembicConfig, db_url: str) -> None:
    """Refuse to migrate a DB stamped with an unknown revision.

    Reads `alembic_version` before any change is applied. A revision not
    present in this build's script map aborts startup with a clear error;
    the database is never downgraded, rewritten, or recreated.
    """
    script = ScriptDirectory.from_config(cfg)
    known = {rev.revision for rev in script.walk_revisions()}
    engine = create_engine_for(db_url)
    try:
        with engine.connect() as conn:
            heads = MigrationContext.configure(conn).get_current_heads()
    finally:
        engine.dispose()
    unknown = sorted(h for h in heads if h not in known)
    if unknown:
        raise UnsupportedRevisionError(
            f"database revision {unknown} is not recognized by this build — "
            f"it was likely written by a newer version of Atomic Dream "
            f"Engine. Update the app or set ATOMIC_DATA_DIR to a different "
            f"directory. The database was not modified."
        )


def run_migrations(db_url: str) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_alembic_script_location()))
    cfg.set_main_option("sqlalchemy.url", db_url)
    _assert_supported_revision(cfg, db_url)
    alembic_command.upgrade(cfg, "head")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    ensure_dirs(settings)
    configure_logging(settings.log_level, settings.logs_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Exclusive data-dir ownership comes first: a second backend
        # exits here before it can migrate, sweep, reconcile, or spawn.
        lock = acquire_data_dir_lock(settings.data_dir)
        app.state.data_dir_lock = lock
        engine = None
        try:
            run_migrations(settings.db_url)
            engine = create_engine_for(settings.db_url)
            sf = session_factory(engine)
            app.state.engine = engine
            app.state.session_factory = sf
            app.state.settings = settings
            app.state.event_bus = EventBus()
            # A2.1 — bounded unauthenticated WS connection pool: the
            # counter is only mutated under this lock so concurrent
            # handshakes can never exceed WS_MAX_UNAUTHENTICATED.
            import asyncio as _asyncio
            app.state.ws_unauth_lock = _asyncio.Lock()
            app.state.ws_unauth_count = 0

            # C1 provider credential vault — DPAPI-protected file under
            # the data dir; secrets never enter the DB.
            from app.domain.providers.credentials import CredentialVault
            app.state.provider_vault = CredentialVault(
                settings.data_dir / "credentials.vault")

            from app.domain.storage import StorageRegistry
            from app.domain.backup import sweep_backup_destinations, sweep_staging
            storage = StorageRegistry(sf, settings.data_dir)
            app.state.storage = storage
            with sf() as s:
                storage.ensure_default_location(s)
                storage.probe_locations(s)
                swept = storage.sweep(s)
                staging_removed = sweep_staging(settings.data_dir)
                staging_removed += sweep_backup_destinations(s, settings)
                if swept["tmp_removed"] or staging_removed:
                    log.info("storage sweep: %d tmp blobs, %d staging dirs removed",
                             swept["tmp_removed"], staging_removed)
                # C2.0b — rotate the billing session every app run so a
                # 'session' budget scopes to this launch. The id is
                # persisted for historical attribution of ledger rows.
                from app.domain import spend as _spend
                app.state.billing_session_id = _spend.rotate_billing_session(s)
                s.commit()

            from app.domain import gpu_sched, hardware
            from app.domain.model_files import _get_setting

            def _gpu_prefs() -> dict:
                with sf() as s:
                    return {
                        "mode": _get_setting(s, "gpu_mode") or "auto",
                        "preferred_gpu": _get_setting(s, "preferred_gpu"),
                    }

            app.state.gpu_scheduler = gpu_sched.GpuScheduler(
                hw_probe=hardware.probe, prefs=_gpu_prefs)

            # M4.1 persistent-worker pool — Phase 1 serves the mock
            # persistent adapter only; real runtimes arrive in Phase 2.
            import psutil as _psutil
            from app.domain.workers.pool import WorkerPool
            from app.domain.workers.resources import ResidencyRegistry

            def _gpu_free(idx: int) -> int | None:
                for g in app.state.gpu_scheduler.gpus():
                    if g.get("index") == idx:
                        return g.get("vram_free_bytes")
                return None

            residency = ResidencyRegistry(
                ram_free_bytes=lambda: _psutil.virtual_memory().available,
                gpu_free_bytes=_gpu_free,
            )
            app.state.worker_pool = WorkerPool(
                residency,
                max_total=settings.worker_pool_max,
                max_per_gpu=settings.worker_pool_per_gpu,
                idle_timeout_s=settings.worker_idle_s,
            )
            await app.state.worker_pool.start()

            app.state.adapter_registry = AdapterRegistry(
                session_factory=sf, storage=storage, settings=settings,
                worker_pool=app.state.worker_pool,
            )
            job_engine = JobEngine(sf, app.state.adapter_registry, app.state.event_bus,
                                   settings.data_dir, gpu_scheduler=app.state.gpu_scheduler)
            app.state.job_engine = job_engine
            with sf() as s:
                requeued = job_engine.reconcile_interrupted(s)
                if requeued:
                    log.info("re-queued %d interrupted job(s)", requeued)
                # Backup runs left 'running' by a crash are failed honestly.
                from app.db.models import BackupRun
                from sqlalchemy import select as _select
                for run in s.execute(_select(BackupRun).where(BackupRun.status == "running")).scalars():
                    run.status = "failed"
                    run.error = "interrupted by application stop"
                s.commit()
            await job_engine.start()
            # S2 — staged-upload retention sweep, after job reconcile so
            # recoverable jobs' referenced staging files are protected.
            from app.domain.upload_staging import sweep_all_staged
            sweep_all_staged(sf, settings.data_dir)
            # C1.5 stage B — one shared polling task for opted-in providers
            # (settings.import_behavior == "poll"). In-memory candidates
            # only; every filesystem touch stays on its single worker
            # thread. Disabled by default — persisted opt-in re-arms after
            # restart but never implies files were monitored while stopped.
            app.state.watch_service = ImportWatchService(sf)
            await app.state.watch_service.start()
            log.info("backend ready on %s:%s (data: %s)", settings.host, settings.port, settings.data_dir)
            yield
            await app.state.watch_service.stop()
            await job_engine.stop()
            # Workers are drained after the engine stops so no adapter
            # still holds a lease; Job Objects cover abrupt deaths anyway.
            pool = getattr(app.state, "worker_pool", None)
            if pool is not None:
                await pool.stop()
        finally:
            if engine is not None:
                engine.dispose()
            lock.release()

    app = FastAPI(title="Atomic Dream Engine Backend", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(open_router)
    app.include_router(api_router, dependencies=[Depends(require_token)])
    app.include_router(providers_router, dependencies=[Depends(require_token)])
    app.websocket("/api/ws")(websocket_endpoint)
    return app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Atomic Dream Engine backend sidecar")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--token", default=None)
    p.add_argument("--data-dir", default=None)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    if args.host:
        settings.host = args.host
    if args.port is not None:
        settings.port = args.port
    if args.token:
        settings.session_token = args.token
    if args.data_dir:
        settings.data_dir = Path(args.data_dir)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
