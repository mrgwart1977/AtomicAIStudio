"""Provider adapter contract.

UI never calls providers directly; jobs go through the engine, which
asks an adapter for its declared capability profile, validates the
request against it, and streams progress. Real inference adapters
arrive from Milestone 3 onward and must be verified on target hardware
before they are enabled — mock adapters prove orchestration only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


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
    notes: str = ""


@dataclass
class JobHandle:
    """Runtime view handed to an adapter for one job execution."""

    job_id: str
    project_id: str
    request: dict[str, Any]
    work_dir: str


class ProviderAdapter(Protocol):
    profile: CapabilityProfile

    async def validate(self, request: dict[str, Any]) -> list[str]:
        """Return a list of capability violations; empty means runnable."""
        ...

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        """Yield progress dicts; final yield must include {"done": True, "output": {...}}."""
        ...
