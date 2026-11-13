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
import hashlib
import json
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
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

# Independent owner-authorization boundary — an authenticated app
# session is NOT proof of owner approval. Authorization is granted by
# the owner writing an approval file DIRECTLY under
# ``<data_dir>/owner/`` with a text editor — no command, API route, env
# var, settings toggle, or database row can create one, and project
# backup/restore never exports it. Revocation = delete the file or let
# ``not_after`` lapse; both take effect on the next attempt, fail
# closed.
#
#   owner/arm-approval.json
#       {"approve": "validate_arm",
#        "provider_key": "deepseek", "model_id": "deepseek-chat",
#        "project_id": "<project id>",
#        "not_after": "<ISO-8601 UTC>"}
#
#   owner/exec-approval.json   — the Stage B2b execution lock.
#       {"approve": "validate_run",
#        "permit_fingerprint": "<sha256 of the armed permit>",
#        "not_after": "<ISO-8601 UTC>"}
#
#   The exec approval is bound to the FINGERPRINT of the armed permit:
#   a stale approval, a re-armed permit, or a restored database can
#   never satisfy it. Nothing in this build creates an exec approval —
#   Stage B2b stays locked until the owner writes one.
OWNER_DIR = "owner"
_APPROVAL_SCOPES = {"arm": "validate_arm", "exec": "validate_run"}


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
# owner approval — independent authorization boundary
# ------------------------------------------------------------------

def permit_fingerprint(permit: dict[str, Any]) -> str:
    """sha256 over the canonical armed record — the exec approval must
    name this exact fingerprint, so it can only ever authorize THIS
    permit."""
    canon = json.dumps(permit, sort_keys=True, separators=(",", ":"),
                       default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


def approval_path(data_dir: Any, kind: str) -> Path:
    return Path(data_dir) / OWNER_DIR / f"{kind}-approval.json"


def load_approval(data_dir: Any, kind: str) -> dict[str, Any]:
    """Read + validate an owner-approval file. Fail closed on EVERY
    defect — missing file, unreadable, malformed JSON, wrong scope,
    missing or lapsed expiry."""
    if data_dir is None:
        raise PermitRefused(
            f"owner_approval:{kind}",
            "no data dir — owner approval cannot be established")
    path = approval_path(data_dir, kind)
    if not path.is_file():
        raise PermitRefused(
            f"owner_approval:{kind}",
            f"no owner {kind} approval file — an authenticated session "
            "is not owner authorization; see module docstring")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PermitRefused(
            f"owner_approval:{kind}",
            f"owner {kind} approval unreadable/malformed") from exc
    if not isinstance(doc, dict):
        raise PermitRefused(
            f"owner_approval:{kind}", "approval document malformed")
    if doc.get("approve") != _APPROVAL_SCOPES[kind]:
        raise PermitRefused(
            f"owner_approval:{kind}",
            f"approval scope must be '{_APPROVAL_SCOPES[kind]}'")
    not_after = doc.get("not_after")
    try:
        expiry = datetime.fromisoformat(str(not_after))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise PermitRefused(
            f"owner_approval:{kind}",
            "approval requires an ISO 'not_after' expiry") from exc
    if expiry <= datetime.now(timezone.utc):
        raise PermitRefused(
            f"owner_approval:{kind}", "owner approval has expired")
    return doc


# ------------------------------------------------------------------
# arm — the authorization act (audited command only)
# ------------------------------------------------------------------

def arm(session: Session, *, project_id: str, credential_ref: str,
        prompt: Any, max_output_tokens: Any, max_cost_usd: Any,
        command_id: str | None, vault: Any,
        data_dir: Any | None = None) -> dict[str, Any]:
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
    # Independent owner authorization — checked AFTER every app-level
    # check so the approval is the final gate, not a substitute for it.
    doc = load_approval(data_dir, "arm")
    for f, want in (("provider_key", PERMIT_PROVIDER),
                    ("model_id", PERMIT_MODEL),
                    ("project_id", project_id)):
        if doc.get(f) != want:
            raise PermitRefused(
                f"owner_approval:{f}",
                f"owner arm-approval {f} does not match this request")
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
            dispatch: dict[str, Any],
            data_dir: Any | None = None) -> dict[str, Any]:
    """Move the armed permit to consumed — same transaction as the
    reservation booking. Field-by-field compare against what the
    dispatch will actually use; any mismatch refuses WITHOUT consuming
    (the armed record stays intact as evidence).

    The Stage B2b execution lock lives here: a valid owner
    ``exec-approval`` naming THIS permit's fingerprint must exist —
    an armed permit alone can never execute."""
    st = _state(session)
    permit = st["armed"]
    if permit is None:
        raise PermitRefused(
            "no_permit",
            "no armed validation permit — dispatch refused")
    # Lock check FIRST — absent/expired/mis-scoped exec approval
    # refuses before anything else is compared.
    doc = load_approval(data_dir, "exec")
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
    # Fingerprint binding LAST — the approval must name this exact
    # armed record, so a swapped/rewritten permit can never satisfy a
    # previously issued approval.
    if doc.get("permit_fingerprint") != permit_fingerprint(permit):
        raise PermitRefused(
            "owner_approval:fingerprint",
            "exec approval is not bound to the armed permit — refused, "
            "permit stays armed")
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
                   data_dir: Any | None = None,
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

    Durable state machine on the GenerationJob row (created atomically
    with the command, status "validating"):

        validating → dispatching → succeeded | failed | unknown

    "validating" and "dispatching" are deliberately outside the engine
    vocabulary — nothing selects them for scheduling, the restart sweep
    only touches "running", and resumable=False plus an unregistered
    adapter_id mean a restart can never resubmit. A crash freezes the
    row at its last committed state — that IS the audit:
      * validating  — accepted, dispatch never started.
      * dispatching — died inside the boundary; if a reservation was
                      booked it stays OPEN for explicit reconcile.
      * unknown     — non-terminal: provider outcome undetermined,
                      reservation held.

    inner=None (the shipped path) builds the real _SecureHTTPTransport;
    tests inject MockTransport/dummy vaults — and is_real=False means
    no controlled run can produce verified_remote.
    """
    out: dict[str, Any] = {}

    def _mark(s, job_id, status, error=None, result=None):
        from app.db.models import GenerationJob
        job = s.get(GenerationJob, job_id)
        if job is not None:
            job.status = status
            if error is not None:
                job.error = error
            if result is not None:
                job.result_json = result
            s.commit()

    def _open_reservation(s, job_id) -> bool:
        from app.db.models import SpendReservation
        from sqlalchemy import select
        row = s.execute(
            select(SpendReservation).where(
                SpendReservation.job_id == job_id)
        ).scalar_one_or_none()
        return row is not None and row.status == "open"

    def work() -> None:
        try:
            with session_factory() as s:
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
                _mark(s, job_id, "dispatching")
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
                        expected_permit=permit, data_dir=data_dir))
                except Exception:
                    # If a reservation committed before the refusal the
                    # outcome is unknown — mark it held, not failed.
                    s.rollback()
                    held = _open_reservation(s, job_id)
                    _mark(
                        s, job_id,
                        "unknown" if held else "failed",
                        error="dispatch refused — reservation held open"
                              " for reconcile" if held else
                              "dispatch refused before admission")
                    raise
                outcome = out["result"]["outcome"]
                if outcome == "succeeded":
                    _mark(s, job_id, "succeeded",
                          result={"outcome": outcome})
                elif outcome == "ambiguous":
                    _mark(s, job_id, "unknown",
                          error="provider outcome undetermined — "
                                "reservation held for reconcile",
                          result={"outcome": outcome})
                else:
                    _mark(s, job_id, "failed",
                          error=f"{outcome}: "
                                f"{out['result']['reservation']}",
                          result={"outcome": outcome})
        except Exception as exc:
            out["error"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join()
    if "error" in out:
        raise out["error"]
    return out["result"]
