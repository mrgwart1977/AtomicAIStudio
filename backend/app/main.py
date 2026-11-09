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
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.registry import AdapterRegistry
from app.api.routers import api_router, open_router, websocket_endpoint
from app.config import Settings, ensure_dirs, get_settings
from app.db.session import create_engine_for, session_factory
from app.domain.events import EventBus
from app.domain.jobs import JobEngine
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


def run_migrations(db_url: str) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_alembic_script_location()))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    ensure_dirs(settings)
    configure_logging(settings.log_level, settings.logs_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        run_migrations(settings.db_url)
        engine = create_engine_for(settings.db_url)
        sf = session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = sf
        app.state.settings = settings
        app.state.event_bus = EventBus()

        from app.domain.storage import StorageRegistry
        from app.domain.backup import sweep_staging
        storage = StorageRegistry(sf, settings.data_dir)
        app.state.storage = storage
        with sf() as s:
            storage.ensure_default_location(s)
            storage.probe_locations(s)
            swept = storage.sweep(s)
            staging_removed = sweep_staging(settings.data_dir)
            if swept["tmp_removed"] or staging_removed:
                log.info("storage sweep: %d tmp blobs, %d staging dirs removed",
                         swept["tmp_removed"], staging_removed)
            s.commit()

        app.state.adapter_registry = AdapterRegistry(
            session_factory=sf, storage=storage, settings=settings
        )
        job_engine = JobEngine(sf, app.state.adapter_registry, app.state.event_bus, settings.data_dir)
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
        log.info("backend ready on %s:%s (data: %s)", settings.host, settings.port, settings.data_dir)
        yield
        await job_engine.stop()
        engine.dispose()

    app = FastAPI(title="Atomic AI Studio Backend", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(open_router)
    app.include_router(api_router, dependencies=[Depends(require_token)])
    app.websocket("/api/ws")(websocket_endpoint)
    return app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Atomic AI Studio backend sidecar")
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
