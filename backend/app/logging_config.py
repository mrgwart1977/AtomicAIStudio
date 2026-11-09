"""Logging setup: console + rotating file under data_dir/logs."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler


def configure_logging(level: str, log_dir) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")

    root = logging.getLogger()
    root.setLevel(level.upper())

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "atomic-backend.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
