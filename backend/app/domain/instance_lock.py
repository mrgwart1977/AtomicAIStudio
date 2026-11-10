"""Exclusive ownership of a data directory.

A backend must own the data directory before touching the database,
running migrations, sweeping storage, or spawning workers. Ownership is
an OS byte-range lock on ``<data_dir>/.atomic.lock`` held for the
process lifetime; the OS releases it when the process exits or crashes,
so no cleanup or stale-file handling is ever required.

The lock FILE is not the lock: a leftover file proves nothing and must
never be deleted or treated as "in use". Ownership is only ever
established by acquiring the OS lock on it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

LOCK_FILENAME = ".atomic.lock"


class DataDirLockError(RuntimeError):
    """Another backend already owns this data directory."""


# Windows byte-range locks do not conflict within one process, so an
# in-process registry guards a second app instance started from the same
# interpreter (e.g. a second TestClient on the same data dir in tests).
_HELD: dict[Path, "DataDirLock"] = {}


def _lock_bytes(fh) -> None:
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"atomic dream engine data-directory lock\n")
        fh.flush()
    fh.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_bytes(fh) -> None:
    try:
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class DataDirLock:
    """Handle for the held OS lock; release() is idempotent and the OS
    also releases automatically if the process dies without it."""

    def __init__(self, fh, root: Path):
        self._fh = fh
        self.root = root
        self.path = root / LOCK_FILENAME

    def release(self) -> None:
        if self._fh is None:
            return
        fh, self._fh = self._fh, None
        try:
            _unlock_bytes(fh)
        finally:
            fh.close()
            _HELD.pop(self.root, None)

    def __enter__(self) -> "DataDirLock":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def acquire_data_dir_lock(data_dir: Path) -> DataDirLock:
    """Take exclusive ownership of ``data_dir`` or raise DataDirLockError.

    Never deletes or inspects the lock file's contents — existence alone
    means nothing; only a successful OS lock grants ownership.
    """
    root = data_dir.resolve()
    if root in _HELD:
        raise DataDirLockError(
            f"data directory is already in use in this process: {root}"
        )
    root.mkdir(parents=True, exist_ok=True)
    fh = open(root / LOCK_FILENAME, "a+b")
    try:
        _lock_bytes(fh)
    except OSError as e:
        fh.close()
        raise DataDirLockError(
            f"data directory is already in use by another Atomic Dream "
            f"Engine instance: {root} — close the other instance or set "
            f"ATOMIC_DATA_DIR to a different directory."
        ) from e
    lock = DataDirLock(fh, root)
    _HELD[root] = lock
    return lock
