"""Adapter registry. Adapters are registered explicitly; nothing is
auto-enabled. Capability profiles are exposed to the UI verbatim so the
interface can gate controls honestly."""

from __future__ import annotations

from app.adapters.base import ProviderAdapter
from app.adapters.mock import MockImageAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self.register(MockImageAdapter())

    def register(self, adapter: ProviderAdapter) -> None:
        self._adapters[adapter.profile.adapter_id] = adapter

    def get(self, adapter_id: str) -> ProviderAdapter | None:
        return self._adapters.get(adapter_id)

    def list_profiles(self) -> list[dict]:
        return [
            {
                "adapter_id": a.profile.adapter_id,
                "display_name": a.profile.display_name,
                "adapter_kind": a.profile.adapter_kind,
                "tasks": list(a.profile.tasks),
                "supports": a.profile.supports,
                "limits": a.profile.limits,
                "requires_cloud": a.profile.requires_cloud,
                "cost_model": a.profile.cost_model,
                "notes": a.profile.notes,
            }
            for a in self._adapters.values()
        ]
