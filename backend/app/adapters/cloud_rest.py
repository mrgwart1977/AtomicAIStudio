"""C2.0c — RestCloudTextAdapter: documented provider contracts on
injected, in-memory HTTP transports.

Drives the same durable remote-job contract as the mock adapter —
intent persisted before submit → remote id where the provider's
lifecycle produces one → bounded poll only where documented → output →
CAS — but the request/response shapes are REAL provider contracts
(OpenAI Responses background mode; Anthropic Messages sync) executed
against httpx.MockTransport. Zero egress: providers refuse non-mock
transports at construction; provenance stays "simulated" because
provider_verified evidence requires a real authorized transport that
does not exist here.

Spend integration is identical to the mock path EXCEPT settlement data:
usage is parsed from the provider-shaped response (simulated fixture
values) and cost is calculated locally from catalog rates — never a
provider-confirmed invoice. The charge source stays "simulated" and
usage_json states the basis.
"""

from __future__ import annotations

import asyncio
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator

from app.adapters.base import CapabilityProfile, JobHandle, JobCancelled
from app.domain import spend as _spend
from app.domain.workers.remote import (RemoteProvider, RemoteStatus,
                                       SubmissionIntent)
from app.domain.workers.rest_providers import (EgressRefused, RemoteError,
                                               RemoteErrorKind,
                                               UnsupportedRemoteOp)

_POLL_INTERVAL_S = 0.1   # test cadence; a live transport would back off
_POLL_BUDGET_S = 30.0

_SIM_BANNER = (
    "SIMULATED TRANSPORT OUTPUT — provider-shaped response produced by\n"
    "an in-memory httpx.MockTransport fixture. No provider was contacted;\n"
    "this text is synthetic and must never be presented as real output.\n"
)


def _usage_cost_micro(usage: dict | None, cost_table: dict | None) -> tuple[int | None, str]:
    """Exact integer-token usage × catalog rates → micro-USD.

    Decimal throughout; the USD→micro boundary is the shared
    spend.usd_to_micro (ceiling — never understate). Returns
    (micro, basis): basis distinguishes a locally calculated cost from
    any provider-confirmed billed amount (which no text API returns)."""
    table = cost_table or {}
    in_rate, out_rate = table.get("input_per_mtok"), table.get("output_per_mtok")
    if usage is None or in_rate is None or out_rate is None:
        return None, "no_usage_or_rates"
    usd = (Decimal(int(usage["input_tokens"])) * Decimal(str(in_rate))
           + Decimal(int(usage["output_tokens"])) * Decimal(str(out_rate))) / Decimal(1_000_000)
    return _spend.usd_to_micro(usd), "usage_x_catalog_rates"


class RestCloudTextAdapter:
    """Drives documented REST contracts through injected providers."""

    def __init__(self, providers: dict[str, RemoteProvider]) -> None:
        if not providers:
            raise ValueError("RestCloudTextAdapter requires providers")
        for key, p in providers.items():
            # Double-guard: providers must be transport-mocked. A
            # non-mock transport means a real egress path exists and
            # this adapter refuses to be constructed with it.
            if not getattr(p, "transport_is_simulated", False):
                raise EgressRefused(
                    f"provider {key!r} is not transport-mocked — "
                    "C2.0c wires no live path")
        self.providers = dict(providers)

    def supports_provider(self, provider_key: str) -> bool:
        return provider_key in self.providers

    @property
    def spend_environment(self) -> str:
        """Accounting environment derived from the transports actually
        wired — mocked transports account as 'simulated'. In C2.0c this
        is always 'simulated' (construction refuses live transports),
        but the property stays honest if a real transport ever lands."""
        return ("simulated" if all(
            getattr(p, "transport_is_simulated", False)
            for p in self.providers.values()) else "live")

    profile = CapabilityProfile(
        adapter_id="cloud.text.rest",
        display_name="REST Cloud Text (documented contract, simulated transport)",
        adapter_kind="cloud",
        tasks=("text.generate",),
        supports={"model_id": True, "remote_job": True},
        limits={"max_prompt_chars": 8000},
        requires_cloud=True,
        cost_model="per_call",
        lane="gen", needs_gpu=False,
        worker_model="remote",
        execution_scope="remote",
        notes=("Documented OpenAI Responses (background+poll+cancel) and "
               "Anthropic Messages (sync) contracts exercised over "
               "httpx.MockTransport — zero egress, dummy credentials, "
               "simulated accounting; verified_remote unreachable."),
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
            errors.append(f"no REST contract for provider '{key}'")
        return errors

    # ---- internals ----

    @staticmethod
    def _fail_detail(remote_id, outcome, *, spend_action, extra=None):
        return {"remote_job_id": remote_id, "remote_outcome": outcome,
                "reconcile_required": outcome == "ambiguous",
                "spend_action": spend_action,
                "transport_simulated": True, **(extra or {})}

    @staticmethod
    def _settle_action(req, handle, remote_id):
        usage = (handle.detail or {}).get("usage")
        micro, basis = _usage_cost_micro(usage, req.get("cost_table"))
        if micro is None:
            micro = _spend.usd_to_micro(req.get("est_cost_usd") or 0)
            basis = "estimate_at_settle"
        return {"type": "settle", "micro": micro, "source": "simulated",
                "remote_job_id": remote_id,
                "usage": {"usage": usage or {},
                          "usage_reported_by": "provider_response_simulated",
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
                     "prompt_chars": len(req.get("prompt") or ""),
                     "cost_table": req.get("cost_table"),
                     "max_output_tokens": req.get("max_output_tokens")},
            est_cost_usd=req.get("est_cost_usd"),
            max_cost_usd=req.get("est_cost_usd"),
        )

        yield {"progress": 0.02,
               "message": "submission intent recorded",
               "detail": {"remote_intent": {
                   "provider_key": provider_key,
                   "idempotency_key": intent.idempotency_key,
                   "lifecycle": provider.spec.lifecycle,
                   "idempotency_declared": provider.idempotency,
                   "est_cost_usd": intent.est_cost_usd,
                   "is_estimate": True,
                   "simulated": True,
                   "transport_simulated": True}}}

        # Submit — RemoteError.charge_evidence decides hold vs release.
        # Ambiguous outcomes are NEVER retried (idempotency "none" for
        # both providers — the durable job key is the only dedupe).
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
            # A poll lifecycle without a remote operation id is a
            # malformed submission — the remote MAY exist; hold and
            # reconcile rather than poll a fabricated id.
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
            # Documented synchronous completion — the handle IS the
            # finished response. No polling exists to drive.
            for item in self._finish(handle, req, rh,
                                     (rh.detail or {}).get("text", "")):
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
                for item in self._finish(
                        handle, req, h, (h.detail or {}).get("text", "")):
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

    async def _cancel_detail(self, provider, rh, req) -> dict:
        """User cancel → documented remote cancel (best-effort) or an
        honest 'unsupported/ambiguous' when the provider has none or the
        cancel outcome is unclear."""
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
        usage = (h.detail or {}).get("usage")
        if h.status == RemoteStatus.CANCELLED and usage:
            # Cancelled with reported usage → partial charge settles at
            # catalog rates (locally calculated, still simulated).
            micro, basis = _usage_cost_micro(usage, req.get("cost_table"))
            return self._fail_detail(
                rh.remote_id, "cancelled",
                spend_action={"type": "settle",
                              "micro": micro if micro is not None else
                              _spend.usd_to_micro(
                                  req.get("est_cost_usd") or 0),
                              "source": "simulated",
                              "remote_job_id": rh.remote_id,
                              "usage": {"usage": usage,
                                        "usage_reported_by":
                                            "provider_response_simulated",
                                        "cost_basis": basis,
                                        "cost_is_provider_billed": False,
                                        "transport": "mock_transport"}})
        return self._fail_detail(
            rh.remote_id, "cancelled" if h.status == RemoteStatus.CANCELLED
            else "ambiguous",
            spend_action=(
                {"type": "release",
                 "reason": "remote cancel confirmed — no usage reported "
                           "(simulated: no charge)"}
                if h.status == RemoteStatus.CANCELLED else
                {"type": "hold",
                 "note": "cancel response inconclusive — exposure held"}))

    def _finish(self, handle: JobHandle, req, rh, text: str):
        out_dir = Path(handle.work_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{handle.job_id}.txt"
        out_path.write_text(
            _SIM_BANNER + "\n" + (text or "[empty completion]") + "\n",
            encoding="utf-8")
        sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
        action, micro, basis = self._settle_action(req, rh, rh.remote_id)
        yield {
            "done": True, "progress": 1.0,
            "detail": {"remote_job_id": rh.remote_id,
                       "remote_status": "succeeded",
                       "transport_simulated": True,
                       "usage": (rh.detail or {}).get("usage"),
                       "cost_micro": micro, "cost_basis": basis,
                       "cost_is_provider_billed": False,
                       "request_id": (rh.detail or {}).get("request_id"),
                       "spend_action": action},
            "output": {
                "path": str(out_path), "sha256": sha, "kind": "text",
                "name": f"cloud-rest-{handle.job_id[:8]}.txt",
                "origin": "generated",
                "media": {
                    "source": "cloud-rest-simulated", "generated": False,
                    "is_mock": True,
                    "adapter_id": self.profile.adapter_id,
                    "note": "Provider-shaped response from an in-memory "
                            "MockTransport — not real provider output.",
                },
                # No provider_verified — a mocked transport can never
                # produce verification evidence; provenance_confidence()
                # can only return "simulated" for these artifacts.
                "remote": {
                    "provider_key": req["provider_key"],
                    "remote_job_id": rh.remote_id,
                    "remote_id_synthetic": True,
                },
            },
        }
