"""Adapter registry. Adapters are registered explicitly; nothing is
auto-enabled. Capability profiles are exposed to the UI verbatim so the
interface can gate controls honestly.

Two families: generation providers (mock only until verified hardware
adapters arrive in M3+) and local ops adapters (filesystem jobs —
import/export/verify — registered with backend services injected).
"""

from __future__ import annotations

import sys

from app.adapters.base import ProviderAdapter
from app.adapters.inference import ModelDownloadAdapter, SdCppAdapter, SdCppVideoAdapter
from app.adapters.mock import MockImageAdapter
from app.adapters.persistent_mock import PersistentMockAdapter
from app.adapters.ops import (
    AssetImportAdapter,
    AssetRelinkAdapter,
    AssetsVerifyAdapter,
    BackupExportAdapter,
    BackupImportAdapter,
    BackupRestoreAdapter,
)


class AdapterRegistry:
    def __init__(self, session_factory=None, storage=None, settings=None,
                 worker_pool=None) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self.register(MockImageAdapter())
        # C2.0a — mock cloud text: exercises the remote-job contract on
        # an in-memory provider. Zero egress, honest "mock" kind.
        from app.adapters.cloud_mock import MockCloudTextAdapter
        self.register(MockCloudTextAdapter())
        # C2.0c — documented REST contracts (OpenAI Responses bg+poll+
        # cancel, Anthropic Messages sync) over httpx.MockTransport
        # fixtures only. Construction refuses any real transport —
        # there is no live egress path.
        from app.adapters.cloud_rest import RestCloudTextAdapter
        from app.domain.workers.rest_simulated import build_simulated_providers
        self.register(RestCloudTextAdapter(build_simulated_providers()))
        # C3-A — documented image contracts (OpenAI Images sync shape)
        # over httpx.MockTransport fixtures only. Construction refuses
        # any real transport — no live egress path exists here either.
        from app.adapters.cloud_image import SimCloudImageAdapter
        from app.domain.workers.rest_simulated import (
            build_simulated_image_providers)
        self.register(SimCloudImageAdapter(
            build_simulated_image_providers()))
        # C3-D2 — simulated img2img on documented edits contracts only
        # (providers whose EndpointSpec declares edit_path). Smaller set
        # by design: a catalog capability flag cannot enable a mode the
        # contract does not implement.
        from app.adapters.cloud_image import SimCloudImg2ImgAdapter
        from app.domain.workers.rest_simulated import (
            build_simulated_img2img_providers)
        self.register(SimCloudImg2ImgAdapter(
            build_simulated_img2img_providers()))
        # C3-D3 — simulated inpaint/outpaint on documented contracts
        # (providers whose EndpointSpec declares inpaint_path or
        # outpaint_path). Same two-layer fail-closed rule.
        from app.adapters.cloud_image import SimCloudEditAdapter
        from app.domain.workers.rest_simulated import (
            build_simulated_edit_providers)
        self.register(SimCloudEditAdapter(
            build_simulated_edit_providers()))
        # M4.1 mock persistent adapter — dev/test only. Its worker is a
        # Python script spawned via sys.executable, which doesn't exist
        # as a usable interpreter in a frozen build (sys.executable is
        # atomic-backend.exe and tests/mock_worker.py isn't shipped).
        # Registering it there would expose a mock that can only fail.
        if not getattr(sys, "frozen", False):
            self.register(PersistentMockAdapter(worker_pool))
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
            # M4.1 Phase 2A — real persistent sd-server worker. Registered
            # only behind ATOMIC_PERSISTENT_INFERENCE_ENABLED=1:
            # unvalidated persistent inference is not selectable by default.
            if getattr(settings, "persistent_inference_enabled", False):
                from app.adapters.sdcpp_server import SdCppServerAdapter
                self.register(SdCppServerAdapter(
                    session_factory, settings, worker_pool))

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
