"""Remote provider interfaces (M4.1 Phase 1 — interface + mock only).

No real API calls exist anywhere in this file. The contract every
future paid provider must satisfy before authorization:

- Every submission carries a caller-generated ``idempotency_key``; the
  provider MUST dedupe retries on it.
- A submission whose outcome is unknown (timeout/disconnect after the
  request left) maps to ``RemoteStatus.UNKNOWN`` → the job's
  ``recovery_state`` becomes ``needs_reconciliation`` and is NEVER
  auto-resubmitted. Only an explicit, owner-approved retry may create a
  second billable submission.
- ``query(remote_id)`` must return the provider's truth; ``None`` means
  unreachable/unknown — again reconciliation, not resubmission.
- Cancellation is best-effort and separate from refunds.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


class RemoteStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"   # submitted?, unreachable — reconcile, never resubmit


@dataclass(frozen=True)
class SubmissionIntent:
    """Durable record of intent BEFORE a billable call is attempted."""

    job_id: str
    idempotency_key: str
    adapter_id: str
    request: dict[str, Any]
    est_cost_usd: float | None = None
    max_cost_usd: float | None = None


@dataclass
class RemoteHandle:
    provider: str
    remote_id: str | None
    status: RemoteStatus
    cost_usd: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class RemoteProvider(Protocol):
    """Remote-side contract a cloud adapter drives.

    Idempotency contract (C2.0a — per-provider transport mappings land
    in C2.0c): ``idempotency`` declares how the provider dedupes a
    resubmission — ``"native"`` (provider honors the key itself),
    ``"request_id"`` (key becomes the retrievable request id), or
    ``"none"`` (provider cannot dedupe — resubmission is FORBIDDEN;
    UNKNOWN outcomes must reconcile, never resubmit). A provider that
    cannot honor one of these modes must not be submitted to.
    """
    name: str
    idempotency: str = "none"  # native | request_id | none

    async def submit(self, intent: SubmissionIntent) -> RemoteHandle: ...

    async def query(self, remote_id: str) -> RemoteHandle | None:
        """None = outcome unknown/unreachable — reconcile, don't resubmit."""
        ...

    async def cancel(self, remote_id: str) -> RemoteHandle: ...


def provenance_confidence(remote: dict | None) -> str:
    """C2.0a/B1 — verified_remote eligibility contract.

    `verified_remote` may be claimed ONLY when `provider_verified` was
    stamped AND the accompanying evidence validates through
    live_gate.verification_evidence_valid on a genuinely live transport:
    authenticated round trip + approved endpoint identity + job/request
    correlation + response fingerprint + evidence URL + audit linkage.
    A remote_job_id is recorded when the provider supplies one but is
    neither required nor sufficient — legitimate sync responses may not
    expose one, and none is ever fabricated. Simulated/mocked results
    can never claim it. This is the single decision point; adapters
    must not set the label directly."""
    rem = remote or {}
    if rem.get("provider_verified") is True:
        from app.domain import live_gate as _lg
        if _lg.verification_evidence_valid(
                rem.get("verification_evidence"),
                transport_is_live=bool(rem.get("transport_is_live")),
                provider_key=rem.get("provider_key")):
            return "verified_remote"
    return "simulated"


class MockRemoteProvider:
    """Deterministic in-memory provider for reconciliation tests.

    Scripted behaviors per idempotency key prefix:
      "ok-*"      → submit returns RUNNING, query→SUCCEEDED
      "lost-*"    → submit raises ConnectionError (outcome unknown —
                    the handle may exist provider-side; a resubmission
                    with the same idempotency key still dedupes)
      "slow-*"    → stays RUNNING until ``complete()`` is called
    """

    name = "mock-remote"

    def __init__(self) -> None:
        self._handles: dict[str, RemoteHandle] = {}
        self._by_idem: dict[str, str] = {}
        self.submission_log: list[SubmissionIntent] = []

    async def submit(self, intent: SubmissionIntent) -> RemoteHandle:
        self.submission_log.append(intent)
        if intent.idempotency_key in self._by_idem:
            return self._handles[self._by_idem[intent.idempotency_key]]
        remote_id = f"rmt-{uuid.uuid4().hex[:10]}"
        if intent.idempotency_key.startswith("lost-"):
            # Remote side MAY have accepted it; our side never learns.
            self._handles[remote_id] = RemoteHandle(
                self.name, remote_id, RemoteStatus.RUNNING)
            self._by_idem[intent.idempotency_key] = remote_id
            raise ConnectionError("submission outcome unknown (simulated)")
        status = (RemoteStatus.RUNNING if intent.idempotency_key.startswith("slow-")
                  else RemoteStatus.SUCCEEDED)
        h = RemoteHandle(self.name, remote_id, status,
                         cost_usd=intent.est_cost_usd)
        self._handles[remote_id] = h
        self._by_idem[intent.idempotency_key] = remote_id
        return h

    async def query(self, remote_id: str) -> RemoteHandle | None:
        return self._handles.get(remote_id)

    async def cancel(self, remote_id: str) -> RemoteHandle:
        h = self._handles[remote_id]
        if h.status in (RemoteStatus.SUBMITTED, RemoteStatus.RUNNING):
            h.status = RemoteStatus.CANCELLED
        return h

    def complete(self, remote_id: str) -> None:
        self._handles[remote_id].status = RemoteStatus.SUCCEEDED
