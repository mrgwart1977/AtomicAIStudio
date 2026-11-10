"""Instance lock + database revision safety.

Lock-contention tests use real subprocesses — OS byte-range locks do
not conflict between threads of one process, so thread-only tests
could never prove exclusivity.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.instance_lock import (
    LOCK_FILENAME,
    DataDirLockError,
    acquire_data_dir_lock,
)
from app.main import UnsupportedRevisionError, create_app, run_migrations

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_URL_TIMEOUT_S = 30

_HOLDER_SRC = """\
import sys, time
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from app.domain.instance_lock import acquire_data_dir_lock
try:
    acquire_data_dir_lock(Path(sys.argv[1]))
except Exception as e:
    print(f"LOCK-FAIL {type(e).__name__}: {e}", flush=True)
    sys.exit(3)
print("LOCKED", flush=True)
time.sleep(60)
"""


def _spawn_holder(data_dir: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(BACKEND_DIR)}
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SRC, str(data_dir), str(BACKEND_DIR)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )


def _wait_line(proc: subprocess.Popen, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if line:
            return line.strip()
        if proc.poll() is not None:
            return proc.stderr.read().strip() if proc.stderr else ""
        time.sleep(0.05)
    raise TimeoutError("holder produced no output")


def _settings(data_dir: Path) -> Settings:
    return Settings(session_token="t", data_dir=data_dir, log_level="WARNING")


def _alembic_version(db_path: Path) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        try:
            return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()


class TestLockAcquisition:
    def test_first_acquires_second_refused(self, tmp_path):
        lock = acquire_data_dir_lock(tmp_path)
        with pytest.raises(DataDirLockError, match="already in use"):
            acquire_data_dir_lock(tmp_path)
        lock.release()

    def test_release_allows_reacquire(self, tmp_path):
        acquire_data_dir_lock(tmp_path).release()
        acquire_data_dir_lock(tmp_path).release()

    def test_lock_file_alone_is_not_ownership(self, tmp_path):
        # A leftover/garbage lock file must not block startup — the file
        # is never the lock, and it is never deleted.
        lock_path = tmp_path / LOCK_FILENAME
        lock_path.write_bytes(b"junk from an older crash\n")
        lock = acquire_data_dir_lock(tmp_path)
        assert lock_path.exists()
        lock.release()
        assert lock_path.exists()
        assert b"junk from an older crash" in lock_path.read_bytes()

    def test_paths_with_spaces_and_unicode(self, tmp_path):
        d = tmp_path / "daß test  dir"
        lock = acquire_data_dir_lock(d)
        assert (d / LOCK_FILENAME).exists()
        lock.release()


class TestCrossProcess:
    def test_second_process_refused_and_released_on_exit(self, tmp_path):
        holder = _spawn_holder(tmp_path)
        try:
            assert _wait_line(holder) == "LOCKED"
            with pytest.raises(DataDirLockError):
                acquire_data_dir_lock(tmp_path)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        # Holder is gone — the OS released its lock.
        acquire_data_dir_lock(tmp_path).release()

    def test_release_after_forced_kill(self, tmp_path):
        holder = _spawn_holder(tmp_path)
        assert _wait_line(holder) == "LOCKED"
        holder.kill()  # SIGKILL-equivalent on Windows
        holder.wait(timeout=10)
        acquire_data_dir_lock(tmp_path).release()

    def test_second_process_error_message_is_clear(self, tmp_path):
        lock = acquire_data_dir_lock(tmp_path)
        try:
            holder = _spawn_holder(tmp_path)
            out = _wait_line(holder)
            code = holder.wait(timeout=10)
            assert code == 3
            assert "already in use" in out
            assert "ATOMIC_DATA_DIR" in out
        finally:
            lock.release()


class TestBackendOwnership:
    def test_second_backend_refused_before_migration(self, tmp_path):
        d = tmp_path / "data"
        first = create_app(_settings(d))
        with TestClient(first):
            db = d / "atomic.db"
            rev = _alembic_version(db)
            second = create_app(_settings(d))
            with pytest.raises(DataDirLockError):
                with TestClient(second):
                    pass
            # Untouched: no re-migration, no sweep, no job engine.
            assert _alembic_version(db) == rev
        # After the first backend exits, ownership is released.
        third = create_app(_settings(d))
        with TestClient(third):
            pass


class TestRevisionSafety:
    def test_fresh_database_initializes(self, tmp_path):
        db = tmp_path / "atomic.db"
        run_migrations(f"sqlite:///{db.as_posix()}")
        assert _alembic_version(db)

    def test_valid_revision_migrates_idempotently(self, tmp_path):
        db = tmp_path / "atomic.db"
        run_migrations(f"sqlite:///{db.as_posix()}")
        rev = _alembic_version(db)
        run_migrations(f"sqlite:///{db.as_posix()}")
        assert _alembic_version(db) == rev

    def test_unknown_future_revision_refused(self, tmp_path):
        db = tmp_path / "atomic.db"
        run_migrations(f"sqlite:///{db.as_posix()}")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE alembic_version SET version_num='future_9999'")
        conn.commit()
        conn.close()
        with pytest.raises(UnsupportedRevisionError, match="newer version"):
            run_migrations(f"sqlite:///{db.as_posix()}")
        # Database left exactly as it was — never downgraded or recreated.
        assert _alembic_version(db) == "future_9999"
        conn = sqlite3.connect(db)
        try:
            conn.execute("SELECT 1 FROM projects LIMIT 1")
        finally:
            conn.close()

    def test_backend_aborts_on_future_revision(self, tmp_path):
        d = tmp_path / "data"
        run_migrations(f"sqlite:///{(d / 'atomic.db').as_posix()}")
        conn = sqlite3.connect(d / "atomic.db")
        conn.execute("UPDATE alembic_version SET version_num='future_9999'")
        conn.commit()
        conn.close()
        app = create_app(_settings(d))
        with pytest.raises(UnsupportedRevisionError):
            with TestClient(app):
                pass
