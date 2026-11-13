"""C2 exit gate — Stage B2a: single-use DeepSeek validation permit.

The ONE authorization mechanism for a future explicitly approved live
validation request. Hardcoded to exactly one provider/model — this is
deliberately NOT a generic live-execution switch:

    provider: deepseek      model: deepseek-chat

A permit is created by the audited command ``cloud.live.validate_arm``
and consumed by ``cloud.live.validate_run`` — which nothing executes
without an armed permit. Stage B2a ships with ZERO armed permits;
arming is the future owner-approval act.

Permit semantics:
  * Single-use — ``consume()`` marks it in the SAME transaction as the
    spend.admit() reservation, so a crash either leaves both uncommitted
    (permit still armed, no reservation) or both committed (permit
    consumed, reservation booked). A consumed permit can never replay.
  * One armed permit at a time — arming while one is armed refuses.
  * Auditable — arm/consume ride the command system; state lives in an
    AppSetting KV row like the Stage A gate (app-level, never exported
    with project backup/restore).
  * Fail closed — every refusal raises PermitRefused/LiveDispatchRefused
    BEFORE any transport exists; mismatched run parameters refuse
    WITHOUT consuming (the armed record stays as evidence).
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db.models import AppSetting, new_id
from app.domain import live_gate as lg
from app.domain.workers import live_transport as lt

PERMIT_KEY = "live_validation_permit_v1"

# The ONLY provider/model a permit may ever authorize — module
# constants, not payload: no input can widen this.
PERMIT_PROVIDER = "deepseek"
PERMIT_MODEL = "deepseek-chat"
MAX_PERMIT_COST_USD = Decimal("0.05")   # B2a proposed reservation cap
MAX_OUTPUT_TOKENS = 64
MAX_PROMPT_CHARS = 500


class PermitRefused(Exception):
    """Permit-layer refusal — ``code`` is a machine-stable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------
# durable state (AppSetting KV — restart-safe, app-level, never
# exported by project backup/restore)
# ------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state(session: Session) -> dict[str, Any]:
    row = session.get(AppSetting, PERMIT_KEY)
    st = (row.value_json or {}).get("value") if row else None
    return st or {"armed": None, "consumed": []}


def _save(session: Session, st: dict[str, Any]) -> None:
    row = session.get(AppSetting, PERMIT_KEY)
    if row is None:
        row = AppSetting(key=PERMIT_KEY, value_json={})
        session.add(row)
    row.value_json = {"value": st}
    session.flush()


def armed(session: Session) -> dict[str, Any] | None:
    return _state(session)["armed"]


def consumed_history(session: Session) -> list[dict[str, Any]]:
    return list(_state(session)["consumed"])


# ------------------------------------------------------------------
# arm — the authorization act (audited command only)
# ------------------------------------------------------------------

def arm(session: Session, *, project_id: str, credential_ref: str,
        prompt: Any, max_output_tokens: Any, max_cost_usd: Any,
        command_id: str | None, vault: Any) -> dict[str, Any]:
    """Create the armed permit. Validates EVERYTHING that can be
    checked without a socket — the run path re-checks admission again
    inside live_dispatch."""
    if armed(session) is not None:
        raise PermitRefused(
            "already_armed",
            "a validation permit is already armed — it is one at a "
            "time; run it or let it be superseded by a later arm")
    if not isinstance(prompt, str) or not prompt.strip():
        raise PermitRefused("prompt", "fixed minimal prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise PermitRefused(
            "prompt", f"prompt exceeds {MAX_PROMPT_CHARS} chars")
    try:
        tokens = int(max_output_tokens)
    except (TypeError, ValueError):
        raise PermitRefused(
            "max_output_tokens", "output-token limit is required")
    if not 1 <= tokens <= MAX_OUTPUT_TOKENS:
        raise PermitRefused(
            "max_output_tokens",
            f"must be 1..{MAX_OUTPUT_TOKENS} — this is a bounded "
            "validation request, not a workload")
    try:
        cost = Decimal(str(max_cost_usd))
    except (InvalidOperation, TypeError):
        raise PermitRefused("max_cost_usd", "a USD cap is required")
    if cost <= 0 or cost > MAX_PERMIT_COST_USD:
        raise PermitRefused(
            "max_cost_usd",
            f"must be > 0 and <= {MAX_PERMIT_COST_USD} — application "
            "reservation only; it is not a provider billing ceiling")
    ref = str(credential_ref or "").strip()
    if not ref:
        raise PermitRefused("credential_ref", "a vault credential "
                            "reference is required")
    if not vault.status(ref).get("configured"):
        raise PermitRefused(
            "credential_ref",
            f"no configured credential under '{ref}' — metadata check "
            "only, the secret itself is never read here")
    # Full Stage A admission chain — enablement, evidence freshness,
    # budgets, caps, concurrency — must ALREADY pass before arming.
    allowed, code = lg.dispatch_admission(
        session, provider_key=PERMIT_PROVIDER, model_id=PERMIT_MODEL,
        max_cost_usd=cost, vault=vault)
    if not allowed:
        raise PermitRefused(code, f"live admission refused at arm: {code}")
    st = _state(session)
    permit = {
        "permit_id": new_id(),
        "provider_key": PERMIT_PROVIDER,
        "model_id": PERMIT_MODEL,
        "project_id": project_id,
        "credential_ref": ref,
        "prompt": prompt,
        "max_output_tokens": tokens,
        "max_cost_usd": str(cost),
        "armed_at": _now(),
        "armed_command_id": command_id,
    }
    st["armed"] = permit
    _save(session, st)
    return permit


# ------------------------------------------------------------------
# consume — called ONLY by live_dispatch inside the spend.admit
# transaction; atomic with the financial reservation
# ------------------------------------------------------------------

_MATCH_FIELDS = ("provider_key", "model_id", "project_id",
                 "credential_ref", "prompt", "max_output_tokens")


def consume(session: Session, *, job_id: str,
            dispatch: dict[str, Any]) -> dict[str, Any]:
    """Move the armed permit to consumed — same transaction as the
    reservation booking. Field-by-field compare against what the
    dispatch will actually use; any mismatch refuses WITHOUT consuming
    (the armed record stays intact as evidence)."""
    st = _state(session)
    permit = st["armed"]
    if permit is None:
        raise PermitRefused(
            "no_permit",
            "no armed validation permit — dispatch refused")
    for f in _MATCH_FIELDS:
        if dispatch.get(f) != permit.get(f):
            raise PermitRefused(
                f"mismatch:{f}",
                f"dispatch {f} does not match the armed permit — "
                "refused, permit stays armed")
    if Decimal(str(dispatch.get("max_cost_usd") or "0")) != \
            Decimal(str(permit["max_cost_usd"])):
        raise PermitRefused(
            "mismatch:max_cost_usd",
            "dispatch cost cap does not match the armed permit")
    st["armed"] = None
    record = {**permit, "consumed_at": _now(), "consumed_job_id": job_id}
    st["consumed"].append(record)
    _save(session, st)
    return record


# ------------------------------------------------------------------
# run — the only caller surface for live_dispatch
# ------------------------------------------------------------------

def run_validation(session_factory: Callable, *, project_id: str,
                   job_id: str, job_idempotency_key: str,
                   command_id: str | None, vault: Any,
                   app_state: Any | None = None, inner: Any | None = None,
                   resolver: Callable | None = None,
                   gate_probe: Callable | None = None,
                   base_backend: Any | None = None) -> dict[str, Any]:
    """Execute the armed permit through live_dispatch.

    Runs on a dedicated thread with its OWN session: the command layer
    is synchronous inside a running event loop, so the async dispatch
    cannot execute on the caller's thread. Everything the dispatch
    sends comes FROM the armed permit record — caller input can only
    select WHICH permit, never reshape it.

    inner=None (the shipped path) builds the real _SecureHTTPTransport;
    tests inject MockTransport/dummy vaults — and is_real=False means
    no controlled run can produce verified_remote.
    """
    out: dict[str, Any] = {}

    def work() -> None:
        from app.db.models import GenerationJob
        with session_factory() as s:
            try:
                permit = armed(s)
                if permit is None:
                    raise PermitRefused(
                        "no_permit",
                        "no armed validation permit — nothing to run")
                if permit["project_id"] != project_id:
                    raise PermitRefused(
                        "mismatch:project_id",
                        "permit is armed for a different project — "
                        "refused, permit stays armed")
                # Durable job row — committed atomically with the
                # reservation + permit consume inside live_dispatch.
                # resumable=False: a crash can never auto-resubmit a
                # possibly-billable request.
                s.add(GenerationJob(
                    id=job_id, project_id=permit["project_id"],
                    adapter_id="cloud.live.validate", kind="text",
                    idempotency_key=job_idempotency_key,
                    request_json={
                        "provider_key": permit["provider_key"],
                        "model_id": permit["model_id"],
                        "prompt": permit["prompt"],
                        "max_output_tokens":
                            permit["max_output_tokens"]},
                    status="running", resumable=False,
                    command_id=command_id))
                s.flush()
                try:
                    out["result"] = asyncio.run(lt.live_dispatch(
                        s,
                        provider_key=permit["provider_key"],
                        model_id=permit["model_id"],
                        request={"model_id": permit["model_id"],
                                 "prompt": permit["prompt"],
                                 "max_output_tokens":
                                     permit["max_output_tokens"]},
                        job_id=job_id, command_id=command_id,
                        project_id=permit["project_id"],
                        max_cost_usd=permit["max_cost_usd"], vault=vault,
                        app_state=app_state, inner=inner,
                        resolver=resolver, gate_probe=gate_probe,
                        base_backend=base_backend,
                        expected_permit=permit))
                except Exception:
                    # If admission committed before the refusal, the
                    # job row committed with it — mark it so reconcile
                    # finds a clean record instead of a stale 'running'.
                    s.rollback()
                    job = s.get(GenerationJob, job_id)
                    if job is not None:
                        job.status = "failed"
                        job.error = ("dispatch refused — see live-gate "
                                     "audit (reservation may be held "
                                     "for reconcile)")
                        s.commit()
                    raise
                job = s.get(GenerationJob, job_id)
                outcome = out["result"]["outcome"]
                if job is not None:
                    job.status = ("succeeded" if outcome == "succeeded"
                                  else "failed")
                    job.error = (None if outcome == "succeeded"
                                 else f"{outcome}: reservation "
                                      f"{out['result']['reservation']}")
                    s.commit()
            except Exception as exc:
                out["error"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join()
    if "error" in out:
        raise out["error"]
    return out["result"]
