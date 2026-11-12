"""Subprocess probe — run ONLY via test_process_exit_is_bounded.

Builds a real ImportWatchService on a throwaway sqlite DB, stalls its
worker thread on a never-set event (a wedged filesystem call), calls
stop(), then sys.exit(0). If interpreter shutdown tries to join the
stalled worker — as concurrent.futures._python_exit does for
ThreadPoolExecutor regardless of daemon flags — this process never
reaches exit and the parent test fails on timeout. With a raw daemon
thread the worker is abandoned and the process exits promptly.

Run from backend/ so `import app` resolves:
    python tests/_watch_exit_probe.py
"""
import asyncio
import sys
import tempfile
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base
from app.domain import bridge
from app.domain.providers import registry
from app.domain.watch_service import ImportWatchService

e = create_engine("sqlite:///" + tempfile.mktemp(suffix=".db"))
Base.metadata.create_all(e)
sf = sessionmaker(bind=e, expire_on_commit=False)

with sf() as s:
    registry.set_provider_config(
        s, "midjourney",
        {"settings": {"import_dir": tempfile.gettempdir(),
                      "import_behavior": "poll"}})
    s.commit()

never = threading.Event()


def stalled(*_a, **_k):
    never.wait(120)                     # a filesystem call that never returns
    return [], False


bridge.scan_watch_folder = stalled

svc = ImportWatchService(sf, shutdown_grace_s=0.5)
asyncio.run(svc._tick())                # schedules the stalled scan
deadline = threading.Event()
assert svc._pending, "scan was not scheduled"
asyncio.run(svc.stop())                 # must return within its grace
sys.exit(0)                             # must not hang joining the worker
