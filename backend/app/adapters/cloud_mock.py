"""C2.0a — MockCloudTextAdapter: full remote-job lifecycle on a mock.

Drives the `RemoteProvider` contract end-to-end — durable submission
intent → remote_job_id → bounded poll → output → CAS — with ZERO
network egress and zero credentials. Every artifact is honestly
labeled: adapter_kind="mock", provenance "simulated", and remote ids
are synthetic — `verified_remote` is structurally unreachable here
(`provider_verified` evidence can only come from a real, authorized
transport — see workers/remote.provenance_confidence).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, AsyncIterator

from app.adapters.base import CapabilityProfile, JobHandle, JobCancelled
from app.domain.workers.remote import (MockRemoteProvider, RemoteProvider,
                                       RemoteStatus, SubmissionIntent)

_POLL_INTERVAL_S = 0.1   # mock cadence; real providers use bounded backoff
_POLL_BUDGET_S = 30.0    # total wait before UNKNOWN-timeout, never resubmit

_MOCK_BANNER = (
    "MOCK CLOUD OUTPUT — simulated remote generation.\n"
    "No provider was contacted; this text is synthetic test output and\n"
    "must never be presented as real provider output.\n"
)


class MockCloudTextAdapter:
    """Simulates a T1 remote text job on an injectable RemoteProvider."""

    def __init__(self, remote: RemoteProvider | None = None) -> None:
        self._remote: RemoteProvider = remote or MockRemoteProvider()

    profile = CapabilityProfile(
        adapter_id="cloud.text.mock",
        display_name="Mock Cloud Text (simulated remote job)",
        adapter_kind="mock",
        tasks=("text.generate",),
        supports={"model_id": True, "remote_job": True},
        limits={"max_prompt_chars": 8000},
        requires_cloud=False,
        cost_model="free",
        lane="gen", needs_gpu=False,
        worker_model="remote",
        execution_scope="remote",
        notes=("Exercises the remote-job contract on an in-memory mock — "
               "no network, no credentials, no billable work. Output is "
               "simulated and can never carry verified_remote."),
    )

    async def validate(self, request: dict[str, Any]) -> list[str]:
        errors = []
        if not (request.get("prompt") or "").strip():
            errors.append("prompt is required")
        if request.get("execution_target") != "cloud":
            errors.append("execution_target must be explicitly 'cloud'")
        if not request.get("provider_key"):
            errors.append("provider_key is required")
        return errors

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        req = handle.request
        provider_key = req["provider_key"]
        intent = SubmissionIntent(
            job_id=handle.job_id,
            # Durable idempotency: the job row's key is the dedupe key —
            # a retry/restart reuses it, so a resubmission can never
            # duplicate billable work (mock or real).
            idempotency_key=req["job_idempotency_key"],
            adapter_id=self.profile.adapter_id,
            request={"provider_key": provider_key,
                     "model_id": req.get("model_id"),
                     "prompt_chars": len(req.get("prompt") or "")},
            est_cost_usd=req.get("est_cost_usd"),
            max_cost_usd=req.get("est_cost_usd"),
        )

        # 1) Persist submission intent BEFORE any (simulated) submission.
        #    Engine commits progress_json after each yield — durable.
        yield {"progress": 0.02,
               "message": "submission intent recorded",
               "detail": {"remote_intent": {
                   "provider_key": provider_key,
                   "idempotency_key": intent.idempotency_key,
                   "est_cost_usd": intent.est_cost_usd,
                   "is_estimate": True,
                   "simulated": True}}}

        # 2) Submit — an ambiguous outcome means the remote side MAY have
        #    accepted it: fail with UNKNOWN, never auto-resubmit.
        try:
            rh = await self._remote.submit(intent)
        except ConnectionError as exc:
            yield {"progress": 0.05, "message": "submission outcome unknown",
                   "detail": {"remote_status": RemoteStatus.UNKNOWN.value,
                              "remote_submission": "outcome_unknown",
                              # Ambiguous ≠ confirmed failure — the remote
                              # side MAY hold the job; tooling must never
                              # read this as a provider refusal.
                              "remote_outcome": "ambiguous",
                              "reconcile_required": True,
                              # C2.0b — reservation stays open: submit
                              # outcome unknown → possible charge.
                              "spend_action": {
                                  "type": "hold",
                                  "note": "submission outcome unknown — "
                                          "remote may have accepted; "
                                          "exposure held"},
                              "simulated": True}}
            raise RuntimeError(
                f"remote submission outcome unknown ({exc}) — not resubmitted")

        yield {"progress": 0.10,
               "message": "remote job submitted (simulated)",
               "detail": {"remote_job_id": rh.remote_id,
                          "remote_status": rh.status.value,
                          "simulated": True}}

        # 3) Bounded poll — cancellation cancels the REMOTE job
        #    (best-effort) before propagating.
        waited = 0.0
        while True:
            try:
                handle.check_cancel()
            except JobCancelled:
                # User cancel → best-effort remote cancel. A CONFIRMED
                # remote cancel releases the reservation (mock contract:
                # cancelled-before-billing); an unreachable remote keeps
                # it open — the charge state is then genuinely unknown.
                released = False
                try:
                    released = bool(await self._remote.cancel(rh.remote_id))
                except Exception:
                    released = False
                yield {"progress": 0.95, "message": "cancelled",
                       "detail": {"remote_job_id": rh.remote_id,
                                  "remote_outcome": ("cancelled" if released
                                                     else "ambiguous"),
                                  "spend_action": (
                                      {"type": "release",
                                       "reason": "remote cancel confirmed "
                                                 "(mock: no charge)"}
                                      if released else
                                      {"type": "hold",
                                       "note": "cancel requested but remote "
                                               "cancel unconfirmed — "
                                               "exposure held"}),
                                  "reconcile_required": not released,
                                  "simulated": True}}
                raise
            h = await self._remote.query(rh.remote_id)
            if h is None:
                yield {"progress": 0.5, "message": "remote outcome unknown",
                       "detail": {"remote_status": RemoteStatus.UNKNOWN.value,
                                  "remote_job_id": rh.remote_id,
                                  "remote_outcome": "ambiguous",
                                  "reconcile_required": True,
                                  "spend_action": {
                                      "type": "hold",
                                      "note": "remote job unreachable — "
                                              "outcome unknown; exposure held"},
                                  "simulated": True}}
                raise RuntimeError(
                    f"remote job {rh.remote_id} unreachable — outcome "
                    "unknown, not resubmitted")
            if h.status == RemoteStatus.SUCCEEDED:
                break
            if h.status == RemoteStatus.FAILED:
                # Confirmed provider-side failure — recorded distinctly
                # from ambiguous outcomes before the job fails. Billing
                # outcome is NOT thereby confirmed (provider may bill
                # partial usage) — the reservation stays open.
                yield {"progress": 0.9, "message": "remote job failed",
                       "detail": {"remote_status": "failed",
                                  "remote_job_id": rh.remote_id,
                                  "remote_outcome": "provider_failed",
                                  "spend_action": {
                                      "type": "hold",
                                      "note": "provider reported failure — "
                                              "billing outcome unknown; "
                                              "exposure held"},
                                  "simulated": True}}
                raise RuntimeError(
                    f"remote job {rh.remote_id} failed "
                    f"({(h.detail or {}).get('error', 'provider error')})")
            if h.status == RemoteStatus.CANCELLED:
                # Provider-side cancel confirmed → confirmed no-charge.
                yield {"progress": 0.95, "message": "remote job cancelled",
                       "detail": {"remote_status": "cancelled",
                                  "remote_job_id": rh.remote_id,
                                  "remote_outcome": "cancelled",
                                  "spend_action": {
                                      "type": "release",
                                      "reason": "remote job confirmed "
                                                "cancelled (mock: no charge)"},
                                  "simulated": True}}
                raise JobCancelled()
            if waited >= _POLL_BUDGET_S:
                yield {"progress": 0.9, "message": "poll timeout",
                       "detail": {"remote_status": "timeout_awaiting",
                                  "remote_job_id": rh.remote_id,
                                  "remote_outcome": "ambiguous",
                                  "reconcile_required": True,
                                  "spend_action": {
                                      "type": "hold",
                                      "note": "poll timeout — remote may "
                                              "still bill; exposure held"},
                                  "simulated": True}}
                raise TimeoutError(
                    f"remote job {rh.remote_id} still running after "
                    f"{_POLL_BUDGET_S:.0f}s — outcome unknown, not resubmitted")
            await asyncio.sleep(_POLL_INTERVAL_S)
            waited += _POLL_INTERVAL_S
            yield {"progress": min(0.10 + waited / _POLL_BUDGET_S * 0.8, 0.9),
                   "message": "remote job running (simulated)",
                   "detail": {"remote_job_id": rh.remote_id,
                              "remote_status": h.status.value,
                              "simulated": True}}

        # 4) Synthesize clearly-labeled output -> CAS via the normal
        #    pipeline (same validation path as every generated asset).
        out_dir = Path(handle.work_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{handle.job_id}.txt"
        body = (_MOCK_BANNER + "\n"
                f"Simulated remote job: {rh.remote_id}\n"
                f"Provider key: {provider_key} (no real contact)\n"
                f"Model: {req.get('model_id')}\n"
                f"Prompt ({len(req.get('prompt') or '')} chars): "
                f"{(req.get('prompt') or '')[:200]}\n\n"
                f"[synthetic completion — deterministic test output]")
        out_path.write_text(body, encoding="utf-8")
        sha = hashlib.sha256(out_path.read_bytes()).hexdigest()

        yield {
            "done": True, "progress": 1.0,
            "detail": {"remote_job_id": rh.remote_id,
                       "remote_status": "succeeded", "simulated": True,
                       "cost_reported": h.cost_usd,
                       "cost_is_simulated": True,
                       # C2.0b — settle the reservation with a SIMULATED
                       # charge (est amount, honestly labeled; never
                       # presented as real provider billing).
                       "spend_action": {
                           "type": "settle",
                           "usd": req.get("est_cost_usd"),
                           "source": "simulated",
                           "remote_job_id": rh.remote_id,
                           "usage": {"simulated_units": 1,
                                     "note": "mock usage — synthetic"}},},
            "output": {
                "path": str(out_path), "sha256": sha, "kind": "text",
                "name": f"cloud-mock-{handle.job_id[:8]}.txt",
                "origin": "generated",
                "media": {
                    "source": "cloud-mock", "generated": False,
                    "is_mock": True,
                    "adapter_id": self.profile.adapter_id,
                    "note": "Simulated remote text output — not real "
                            "provider output.",
                },
                # Remote provenance input — `provider_verified` is absent
                # by design; provenance_confidence() can only return
                # "simulated" for this adapter's artifacts.
                "remote": {
                    "provider_key": provider_key,
                    "remote_job_id": rh.remote_id,
                    "remote_id_synthetic": True,
                },
            },
        }
