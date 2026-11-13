"""C3-A — SimCloudImageAdapter: documented image-provider contracts on
injected, in-memory HTTP transports.

Drives the same durable remote-job contract as the text REST adapter —
intent persisted before submit → classified submit → lifecycle →
safety-inspected image bytes → CAS — but the payload is binary image
data produced by an httpx.MockTransport fixture. Zero egress: providers
refuse non-mock transports at construction; provenance stays
"simulated" because provider_verified evidence requires a real,
authorized transport that does not exist here.

Architecture note (future providers): the lifecycle loop is already
generic — EndpointSpec.lifecycle="async_poll" providers (FLUX-style)
drive the same bounded poll/cancel/timeout path as OpenAI Responses
text; ``Temporary URL`` payloads (``image_url``) parse identically —
a real transport would download them inside the bounded stream limits
(C3-A sim fixtures always return b64). Sync providers return from
``_finish`` immediately. Billing usage is normalized per-provider and
mapped through the extensible image cost units — a provider that cannot
report usable usage leaves the reservation OPEN (usage_unavailable
posture), never a fabricated charge.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator

from app.adapters.base import CapabilityProfile, JobHandle, JobCancelled
from app.domain import image_safety
from app.domain import spend as _spend
from app.domain.cloud_cost import image_tier_rate
from app.domain.workers.remote import (RemoteProvider, RemoteStatus,
                                       SubmissionIntent)
from app.domain.workers.rest_providers import (
    EgressRefused, RemoteError, RemoteErrorKind, UnsupportedRemoteOp)

_POLL_INTERVAL_S = 0.1   # test cadence; a live transport would back off
_POLL_BUDGET_S = 30.0


def _image_cost_micro(usage: dict | None, images: int | None,
                      cost_table: dict | None,
                      quality: str | None) -> tuple[int | None, str]:
    """Provider usage/count × catalog image rates → micro-USD.

    Decimal throughout; USD→micro via spend.usd_to_micro (ceiling).
    Returns (micro, basis). Any unusable basis → (None, reason) — the
    caller HOLDS the reservation rather than inventing a charge."""
    table = cost_table or {}
    unit = table.get("unit")
    if unit == "per_image":
        if images is None:
            return None, "no_images_reported"
        rate = image_tier_rate(table, quality)
        if rate is None:
            return None, "no_image_rate"
        return _spend.usd_to_micro(
            Decimal(str(rate)) * images), "images_x_catalog_rate"
    if unit in ("per_mtok",):
        # Token-billed image models settle from reported token usage.
        if usage is None:
            return None, "no_usage_reported"
        in_r, out_r = table.get("input_per_mtok"), \
            table.get("output_per_mtok")
        if in_r is None or out_r is None:
            return None, "no_token_rates"
        usd = (Decimal(int(usage["input_tokens"])) * Decimal(str(in_r))
               + Decimal(int(usage["output_tokens"])) * Decimal(str(out_r))
               ) / Decimal(1_000_000)
        return _spend.usd_to_micro(usd), "usage_x_catalog_rates"
    if unit == "per_credit":
        # C3-C — credit-metered providers (Recraft API units, BFL
        # credits, Ideogram credits, Firefly generative credits). Settle
        # from the REPORTED credit count when present; otherwise the
        # documented per-image credit price declared in the catalog.
        usd_per_credit = table.get("usd_per_credit")
        if usd_per_credit is None:
            return None, "no_credit_rate"
        credits = (usage or {}).get("credits")
        if credits is None:
            declared = table.get("credits_per_image")
            if declared is None or images is None:
                return None, "no_credits_reported"
            credits = float(declared) * images
        return _spend.usd_to_micro(
            Decimal(str(credits)) * Decimal(str(usd_per_credit))
            ), "credits_x_catalog_rate"
    if unit in ("per_mpixel",):
        # Declared for future providers — settle on reported counts.
        return None, "unit_not_settled_in_c3a"
    return None, "no_cost_table"


class SimCloudImageAdapter:
    """Drives documented image REST contracts through injected providers."""

    def __init__(self, providers: dict[str, RemoteProvider]) -> None:
        if not providers:
            raise ValueError("SimCloudImageAdapter requires providers")
        for key, p in providers.items():
            if not getattr(p, "transport_is_simulated", False):
                raise EgressRefused(
                    f"provider {key!r} is not transport-mocked — "
                    "C3-A wires no live path")
        self.providers = dict(providers)

    def supports_provider(self, provider_key: str) -> bool:
        return provider_key in self.providers

    @property
    def spend_environment(self) -> str:
        return ("simulated" if all(
            getattr(p, "transport_is_simulated", False)
            for p in self.providers.values()) else "live")

    profile = CapabilityProfile(
        adapter_id="cloud.image.sim",
        display_name="Sim Cloud Image (documented contract, simulated transport)",
        adapter_kind="cloud",
        tasks=("image.generate",),
        supports={"model_id": True, "remote_job": True, "seed": True,
                  "size": True, "quality": True},
        limits={"max_prompt_chars": 8000, "images_per_job": 1},
        requires_cloud=True,
        cost_model="per_call",
        lane="gen", needs_gpu=False,
        worker_model="remote",
        execution_scope="remote",
        notes=("Documented image contract exercised over httpx.MockTransport "
               "— OpenAI Images sync shape — zero egress, dummy "
               "credentials, simulated accounting; verified_remote "
               "unreachable. Deterministic test PNGs, not real output."),
    )

    async def validate(self, request: dict[str, Any]) -> list[str]:
        errors = []
        if not (request.get("prompt") or "").strip():
            errors.append("prompt is required")
        if request.get("execution_target") != "cloud":
            errors.append("execution_target must be explicitly 'cloud'")
        key = request.get("provider_key")
        if not key:
            errors.append("provider_key is required")
        elif key not in self.providers:
            errors.append(f"no image contract for provider '{key}'")
        if int(request.get("count") or 1) != 1:
            errors.append("C3-A enforces exactly one image per job")
        return errors

    # ---- internals ----

    @staticmethod
    def _fail_detail(remote_id, outcome, *, spend_action, extra=None):
        return {"remote_job_id": remote_id, "remote_outcome": outcome,
                "reconcile_required": outcome in (
                    "ambiguous", "artifact_pending_retrieval"),
                "spend_action": spend_action,
                "transport_simulated": True, **(extra or {})}

    @staticmethod
    def _settle_action(req, handle, remote_id):
        usage = (handle.detail or {}).get("usage")
        micro, basis = _image_cost_micro(
            usage, (handle.detail or {}).get("images_returned"),
            req.get("cost_table"), req.get("quality"))
        if micro is None:
            # Unusable billing data → exposure stays OPEN for explicit
            # reconcile; never fabricated, never silently released.
            return {"type": "hold",
                    "note": "usage_unavailable — image billing could "
                            "not be determined; exposure held"}, None, basis
        return {"type": "settle", "micro": micro, "source": "simulated",
                "remote_job_id": remote_id,
                "usage": {"usage": usage or {},
                          "images": (handle.detail or {}).get(
                              "images_returned"),
                          "usage_reported_by":
                              "provider_response_simulated",
                          "cost_basis": basis,
                          "cost_is_provider_billed": False,
                          "transport": "mock_transport"}}, micro, basis

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        req = handle.request
        provider_key = req["provider_key"]
        provider = self.providers[provider_key]
        intent = SubmissionIntent(
            job_id=handle.job_id,
            idempotency_key=req["job_idempotency_key"],
            adapter_id=self.profile.adapter_id,
            request={"provider_key": provider_key,
                     "model_id": req.get("model_id"),
                     "prompt": req.get("prompt") or "",
                     "negative_prompt": req.get("negative_prompt"),
                     "size": req.get("size"),
                     "quality": req.get("quality"),
                     "seed": req.get("seed"),
                     "cost_table": req.get("cost_table")},
            est_cost_usd=req.get("est_cost_usd"),
            max_cost_usd=req.get("est_cost_usd"),
        )

        yield {"progress": 0.02,
               "message": "submission intent recorded",
               "detail": {"remote_intent": {
                   "provider_key": provider_key,
                   "idempotency_key": intent.idempotency_key,
                   "lifecycle": provider.spec.lifecycle,
                   "est_cost_usd": intent.est_cost_usd,
                   "is_estimate": True,
                   "simulated": True,
                   "transport_simulated": True}}}

        try:
            rh = await provider.submit(intent)
        except RemoteError as exc:
            rejected = exc.charge_evidence == "none_confirmed"
            yield {"progress": 0.05,
                   "message": ("submission refused by provider"
                               if rejected else
                               "submission outcome unknown"),
                   "detail": self._fail_detail(
                       None,
                       "provider_rejected" if rejected else "ambiguous",
                       spend_action=(
                           {"type": "release",
                            "reason": f"provider refused request "
                                      f"({exc.kind.value}) — no charge"}
                           if rejected else
                           {"type": "hold",
                            "note": f"submit {exc.kind.value} — outcome "
                                    "unknown; exposure held"}),
                       extra={"error_kind": exc.kind.value,
                              "http_status": exc.http_status})}
            raise RuntimeError(
                f"remote submit {exc.kind.value}: {exc}") from exc

        yield {"progress": 0.10,
               "message": "remote job submitted (simulated transport)",
               "detail": {"remote_job_id": rh.remote_id,
                          "remote_status": rh.status.value,
                          "lifecycle": provider.spec.lifecycle,
                          "transport_simulated": True}}

        if provider.spec.lifecycle == "async_poll" and not rh.remote_id:
            yield {"progress": 0.12, "message": "remote outcome unknown",
                   "detail": self._fail_detail(
                       None, "ambiguous",
                       spend_action={"type": "hold",
                                     "note": "submit returned no remote "
                                             "operation id — outcome "
                                             "unknown; exposure held"})}
            raise RuntimeError(
                "async submission returned no remote operation id — "
                "outcome unknown, not resubmitted")

        if provider.spec.lifecycle == "sync":
            async for item in self._finish(handle, req, rh):
                yield item
            return

        # async_poll — bounded poll on the documented retrieval path.
        waited = 0.0
        while True:
            try:
                handle.check_cancel()
            except JobCancelled:
                yield {"progress": 0.95, "message": "cancelled",
                       "detail": await self._cancel_detail(provider, rh, req)}
                raise
            h = await provider.query(rh.remote_id)
            if h is None:
                yield {"progress": 0.5, "message": "remote outcome unknown",
                       "detail": self._fail_detail(
                           rh.remote_id, "ambiguous",
                           spend_action={"type": "hold",
                                         "note": "remote job unreachable "
                                                 "— outcome unknown; "
                                                 "exposure held"})}
                raise RuntimeError(
                    f"remote job {rh.remote_id} unreachable — outcome "
                    "unknown, not resubmitted")
            if h.status == RemoteStatus.SUCCEEDED:
                async for item in self._finish(handle, req, h):
                    yield item
                return
            if h.status == RemoteStatus.FAILED:
                yield {"progress": 0.9, "message": "remote job failed",
                       "detail": self._fail_detail(
                           rh.remote_id, "provider_failed",
                           spend_action={"type": "hold",
                                         "note": "provider reported "
                                                 "failure — billing "
                                                 "outcome unknown; "
                                                 "exposure held"},
                           extra={"remote_status": "failed"})}
                raise RuntimeError(
                    f"remote job {rh.remote_id} failed "
                    f"({(h.detail or {}).get('error', {})})")
            if h.status == RemoteStatus.CANCELLED:
                yield {"progress": 0.95, "message": "remote job cancelled",
                       "detail": self._fail_detail(
                           rh.remote_id, "cancelled",
                           spend_action={"type": "release",
                                         "reason": "remote job confirmed "
                                                  "cancelled (simulated: "
                                                  "no charge)"},
                           extra={"remote_status": "cancelled"})}
                raise JobCancelled()
            if waited >= _POLL_BUDGET_S:
                yield {"progress": 0.9, "message": "poll timeout",
                       "detail": self._fail_detail(
                           rh.remote_id, "ambiguous",
                           spend_action={"type": "hold",
                                         "note": "poll timeout — remote "
                                                 "may still bill; "
                                                 "exposure held"},
                           extra={"remote_status": "timeout_awaiting"})}
                raise TimeoutError(
                    f"remote job {rh.remote_id} still running after "
                    f"{_POLL_BUDGET_S:.0f}s — outcome unknown")
            await asyncio.sleep(_POLL_INTERVAL_S)
            waited += _POLL_INTERVAL_S
            yield {"progress": min(0.10 + waited / _POLL_BUDGET_S * 0.8, 0.9),
                   "message": "remote job running (simulated transport)",
                   "detail": {"remote_job_id": rh.remote_id,
                              "remote_status": h.status.value,
                              "transport_simulated": True}}

    def _reject(self, exc, rh):
        # Provider-shaped but unsafe/unparseable payload — the work may
        # have been processed; billing stays HELD for reconcile.
        yield {"progress": 0.9, "message": "image payload rejected",
               "detail": self._fail_detail(
                   rh.remote_id, "ambiguous",
                   spend_action={"type": "hold",
                                 "note": f"payload failed safety "
                                         f"inspection ({exc}) — "
                                         "outcome unknown; held"},
                   extra={"image_rejected": str(exc)})}

    async def _cancel_detail(self, provider, rh, req) -> dict:
        try:
            h = await provider.cancel(rh.remote_id)
        except UnsupportedRemoteOp:
            return self._fail_detail(
                rh.remote_id, "ambiguous",
                spend_action={"type": "hold",
                              "note": "provider documents no cancel — "
                                      "remote outcome unknown; "
                                      "exposure held"})
        except RemoteError:
            return self._fail_detail(
                rh.remote_id, "ambiguous",
                spend_action={"type": "hold",
                              "note": "remote cancel unconfirmed — "
                                      "exposure held"})
        return self._fail_detail(
            rh.remote_id,
            "cancelled" if h.status == RemoteStatus.CANCELLED
            else "ambiguous",
            spend_action=(
                {"type": "release",
                 "reason": "remote cancel confirmed (simulated: no charge)"}
                if h.status == RemoteStatus.CANCELLED else
                {"type": "hold",
                 "note": "cancel response inconclusive — exposure held"}))

    async def _finish(self, handle: JobHandle, req, rh):
        detail = rh.detail or {}
        b64 = detail.get("image_b64")
        url = detail.get("image_url")
        data: bytes | None = None
        if b64:
            try:
                # Bound the decode BEFORE allocation — base64 inflates
                # ~4/3 over binary; an oversized string refuses before
                # any materialization, independent of the decoded cap.
                if len(b64) > (image_safety.MAX_IMAGE_BYTES * 4 // 3) + 1024:
                    raise image_safety.ImageRejected(
                        f"encoded image payload {len(b64)} chars exceeds "
                        "pre-decode cap")
                data = base64.b64decode(b64)
            except (image_safety.ImageRejected, ValueError) as exc:
                for item in self._reject(exc, rh):
                    yield item
                raise RuntimeError(
                    f"image payload rejected by safety gate: {exc}") from exc
        elif url:
            # C3-C: providers that return temporary artifact URLs (BFL
            # result.sample, Firefly pre-signed outputs, Ideogram
            # data[].url) are retrieved through the provider's own
            # injected transport — the SAME egress boundary as every
            # other call. Failure (expired/undocumented-host/oversize)
            # keeps the durable artifact_pending_retrieval outcome:
            # generation is confirmed, the URL is preserved for
            # reconcile, and nothing is resubmitted.
            provider = self.providers[req["provider_key"]]
            try:
                data = await provider.fetch_artifact(url)
            except (RemoteError, UnsupportedRemoteOp) as exc:
                action, _micro, _basis = self._settle_action(
                    req, rh, rh.remote_id)
                if action["type"] != "settle":
                    action = {"type": "hold",
                              "note": "image generated remotely "
                                      "(temporary URL) — billing "
                                      "unresolved; exposure held for "
                                      "reconcile"}
                yield {"progress": 0.9,
                       "message": "image generated remotely — "
                                  "retrieval failed (reconcile required)",
                       "detail": self._fail_detail(
                           rh.remote_id, "artifact_pending_retrieval",
                           spend_action=action,
                           extra={"remote_status": "succeeded",
                                  "image_url": url,
                                  "retrieval_error": str(exc)})}
                raise RuntimeError(
                    f"remote artifact not retrievable ({exc}); durable "
                    "URL preserved for reconcile, not resubmitted"
                    ) from exc
        else:
            yield {"progress": 0.9, "message": "no inline image bytes",
                   "detail": self._fail_detail(
                       rh.remote_id, "ambiguous",
                       spend_action={"type": "hold",
                                     "note": "response carried no inline "
                                             "image bytes — outcome "
                                             "unknown; exposure held"})}
            raise RuntimeError(
                "image response has no image payload — outcome "
                "unknown, not resubmitted")
        try:
            info = image_safety.inspect_image(data)
        except (image_safety.ImageRejected, ValueError) as exc:
            for item in self._reject(exc, rh):
                yield item
            raise RuntimeError(
                f"image payload rejected by safety gate: {exc}") from exc

        out_dir = Path(handle.work_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{handle.job_id}.png"
        out_path.write_bytes(data)
        thumb = image_safety.make_thumbnail(data)
        thumb_path = out_dir / f"{handle.job_id}-thumb.png"
        thumb_path.write_bytes(thumb)
        thumb_sha = hashlib.sha256(thumb).hexdigest()

        action, micro, basis = self._settle_action(req, rh, rh.remote_id)
        yield {
            "done": True, "progress": 1.0,
            "detail": {"remote_job_id": rh.remote_id,
                       "remote_status": "succeeded",
                       "transport_simulated": True,
                       "usage": detail.get("usage"),
                       "images": detail.get("images_returned"),
                       "cost_micro": micro, "cost_basis": basis,
                       "cost_is_provider_billed": False,
                       "request_id": detail.get("request_id"),
                       "image": info.as_media(),
                       "spend_action": action},
            "output": {
                "path": str(out_path), "sha256": info.sha256,
                "kind": "image", "byte_size": info.byte_size,
                "size": [info.width, info.height],
                "name": f"cloud-sim-{handle.job_id[:8]}.png",
                "origin": "generated",
                "extra_assets": [{
                    "path": str(thumb_path), "sha256": thumb_sha,
                    "kind": "thumbnail", "origin": "generated",
                    "name": f"cloud-sim-{handle.job_id[:8]}-thumb.png",
                }],
                "media": {
                    "source": "cloud-image-simulated",
                    "generated": False, "is_mock": True,
                    "adapter_id": self.profile.adapter_id,
                    "prompt": req.get("prompt"),
                    "negative_prompt": req.get("negative_prompt"),
                    "model_id": req.get("model_id"),
                    "provider_key": req.get("provider_key"),
                    "seed": req.get("seed"),
                    "requested_size": req.get("size"),
                    "actual_width": info.width,
                    "actual_height": info.height,
                    "revised_prompt": detail.get("revised_prompt"),
                    "reference_asset_id": req.get("reference_asset_id"),
                    "scene_id": req.get("scene_id"),
                    "shot_id": req.get("shot_id"),
                    "character_id": req.get("character_id"),
                    "world_id": req.get("world_id"),
                    "location_id": req.get("location_id"),
                    "note": "Provider-shaped response from an in-memory "
                            "MockTransport — deterministic test PNG, "
                            "not real provider output.",
                },
                # No provider_verified — mocked transports can never
                # produce verification evidence; provenance_confidence()
                # can only return "simulated".
                "remote": {
                    "provider_key": req.get("provider_key"),
                    "remote_job_id": rh.remote_id,
                    "remote_id_synthetic": bool(rh.remote_id),
                },
            },
        }
