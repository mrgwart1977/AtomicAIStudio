"""Alembic environment. DB URL comes from ATOMIC_DATABASE_URL, then
settings, then the alembic.ini default — in that order."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from app.db.base import Base
from app.db import models  # noqa: F401  (register tables on metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    if url := os.environ.get("ATOMIC_DATABASE_URL"):
        return url
    if url := config.get_main_option("sqlalchemy.url"):
        return url
    from app.config import get_settings

    return get_settings().db_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from app.db.session import create_engine_for

    engine = create_engine_for(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
