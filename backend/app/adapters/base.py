"""Provider adapter contract.

UI never calls providers directly; jobs go through the engine, which
asks an adapter for its declared capability profile, validates the
request against it, and streams progress. Real inference adapters
arrive from Milestone 3 onward and must be verified on target hardware
before they are enabled — mock adapters prove orchestration only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Protocol


class JobCancelled(Exception):
    """Raised inside an adapter when the user cancels the job. The engine
    converts it to status='cancelled' — never 'failed'."""


@dataclass(frozen=True)
class CapabilityProfile:
    adapter_id: str
    display_name: str
    adapter_kind: str  # "mock" | "local" | "cloud"
    tasks: tuple[str, ...]  # e.g. ("text2image",)
    supports: dict[str, bool] = field(default_factory=dict)  # negative_prompt, seed, size, ...
    limits: dict[str, Any] = field(default_factory=dict)  # max_size, max_duration_s, ...
    requires_cloud: bool = False
    cost_model: str = "free"  # free | per_call | subscription
    # Scheduling lane: "ops" jobs serialize through a single lane (M1.1
    # semantics); "gen" jobs run in parallel up to the GPU-slot count.
    lane: str = "ops"
    # GPU-bound adapters acquire a scheduler slot (or the CPU lane marker)
    # before the engine claims them — a busy GPU leaves the job queued.
    needs_gpu: bool = False
    # M4.1-facing execution contracts — declared, not yet implemented by a
    # worker pool: how execution is hosted, whether mid-flight work can
    # resume, and what happens to unvalidated output.
    worker_model: str = "subprocess-per-job"
    resumable: bool = False
    recoverable_outputs: str = "commit-on-success"
    notes: str = ""


@dataclass
class JobHandle:
    """Runtime view handed to an adapter for one job execution."""

    job_id: str
    project_id: str
    request: dict[str, Any]
    work_dir: str
    cancel_event: threading.Event | None = None

    def check_cancel(self) -> None:
        """Adapters call this at safe checkpoints (file boundaries, chunk
        loops). Cheap — just an Event.is_set()."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobCancelled()

    def cancel_check_callable(self) -> Callable[[], None]:
        """Same check as a no-arg callable for threaded library code."""
        return self.check_cancel


class ProviderAdapter(Protocol):
    profile: CapabilityProfile

    async def validate(self, request: dict[str, Any]) -> list[str]:
        """Return a list of capability violations; empty means runnable."""
        ...

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        """Yield progress dicts; final yield must include {"done": True, "output": {...}}."""
        ...
