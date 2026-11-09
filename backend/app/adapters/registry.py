"""Adapter registry. Adapters are registered explicitly; nothing is
auto-enabled. Capability profiles are exposed to the UI verbatim so the
interface can gate controls honestly.

Two families: generation providers (mock only until verified hardware
adapters arrive in M3+) and local ops adapters (filesystem jobs —
import/export/verify — registered with backend services injected).
"""

from __future__ import annotations

from app.adapters.base import ProviderAdapter
from app.adapters.inference import ModelDownloadAdapter, SdCppAdapter, SdCppVideoAdapter
from app.adapters.mock import MockImageAdapter
from app.adapters.ops import (
    AssetImportAdapter,
    AssetRelinkAdapter,
    AssetsVerifyAdapter,
    BackupExportAdapter,
    BackupImportAdapter,
    BackupRestoreAdapter,
)


class AdapterRegistry:
    def __init__(self, session_factory=None, storage=None, settings=None) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self.register(MockImageAdapter())
        if session_factory is not None and storage is not None:
            for cls in (
                AssetImportAdapter,
                AssetRelinkAdapter,
                AssetsVerifyAdapter,
                BackupExportAdapter,
                BackupImportAdapter,
                BackupRestoreAdapter,
            ):
                self.register(cls(session_factory, storage, settings))
        if session_factory is not None:
            self.register(SdCppAdapter(session_factory, settings))
            self.register(SdCppVideoAdapter(session_factory, settings))
            self.register(ModelDownloadAdapter(session_factory, settings))

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
                "lane": getattr(a.profile, "lane", "ops"),
                "needs_gpu": getattr(a.profile, "needs_gpu", False),
                "notes": a.profile.notes,
            }
            for a in self._adapters.values()
        ]
