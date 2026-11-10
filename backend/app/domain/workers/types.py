"""Worker identity, state, and spawn specification types."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable


class WorkerState(str, enum.Enum):
    """Explicit lifecycle states. Terminal: DEAD (never reuses a proc).

    starting → ready → busy → idle → draining → dead
                  └───────┴── evict/dead on error, crash, or eviction
    """

    STARTING = "starting"
    READY = "ready"          # loaded, awaiting work
    BUSY = "busy"            # executing a request
    IDLE = "idle"            # finished ≥1 request, awaiting more
    DRAINING = "draining"    # asked to stop; finishing/stopping
    DEAD = "dead"            # crashed, killed, or exited — terminal


@dataclass(frozen=True)
class WorkerKey:
    """Identity of a resident worker. Two different keys never share a
    worker process — model, runtime flavor, GPU, and protocol all differ."""

    model_key: str
    runtime_key: str
    flavor: str
    gpu_index: int           # -1 = CPU worker
    protocol: str = "stdio-jsonlines"


@dataclass(frozen=True)
class WorkerSpec:
    """Everything needed to spawn a worker for a WorkerKey.

    argv runs under proc_guard Job-Object supervision. ram/vram estimates
    are the worker's residency cost — conservative upper bounds the
    resource manager charges while the worker lives."""

    key: WorkerKey
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    est_ram_bytes: int = 0
    est_vram_bytes: int = 0
    ready_timeout_s: float = 30.0
    heartbeat_interval_s: float = 5.0
    heartbeat_grace_s: float = 15.0
    # Phase-2 hook: build a non-stdio protocol (e.g. sd-server HTTP) from
    # the spawned process. None → StdioJsonLinesProtocol.
    protocol_factory: Callable[[Any], Any] | None = None


@dataclass
class WorkerInfo:
    """Read-only snapshot for API/UI surfaces."""

    worker_id: str
    key: WorkerKey
    state: WorkerState
    pid: int | None
    gpu_index: int
    ram_bytes: int
    vram_bytes: int
    current_job_id: str | None
    idle_since: float | None
    last_heartbeat: float | None
    requests_completed: int
