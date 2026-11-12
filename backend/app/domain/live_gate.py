"""C2 exit gate — Stage A live-connectivity safety infrastructure.

This module owns the durable "may we ever talk to a real provider?"
decision. Stage A wires NO live dispatch path: every function here is
what a future real transport MUST pass, exercised in tests against
mocked transports and fixture state only. Nothing in this file opens
sockets, builds an httpx client, or reads secret material.

Layers:
  1. Enablement state — durable in the AppSetting KV (restart-safe);
     every mutation rides the command system (cloud.live.*) so grants
     and revocations are audited and project-owned.
  2. dispatch_admission — the complete pre-flight gate a live request
     would have to pass: kill switch, enablement, model grant,
     credential readability (metadata only), evidence freshness,
     per-request cap, one-in-flight-per-provider concurrency, and
     provider/global validation-session exposure bounds.
  3. Egress allowlist — scheme/host checks a Stage-B transport must
     enforce, including redirect targets.
  4. verified_remote evidence contract — the schema a real transport
     must produce; unproducible while no live transport exists.

Hard rules (Stage A and Stage B):
  - No credential material ever leaves the vault — validation uses
    vault.status() metadata only.
  - No automatic resubmission of ambiguous operations, ever.
  - Open reservations survive enable/disable/kill-switch — financial
    rows are never auto-released by this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, SpendBudget, SpendCharge, SpendReservation
from app.domain.providers import catalog
from app.domain.providers.registry import record_stale
from app.domain.spend import micro_to_usd, usd_to_micro
from app.domain.workers.rest_providers import REST_PROVIDER_KEYS, SPECS


class LiveGateRefused(Exception):
    """Refusal carrying a machine-readable code for UI + tests."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


LIVE_GATE_KEY = "cloud_live_gate_v1"

# Only providers with a documented REST contract are even candidates
# for a live lane. Mock/assisted providers can never appear here.
LIVE_PROVIDERS = frozenset(REST_PROVIDER_KEYS)

# Proposed validation-session exposure ceilings (USD) — application-side
# bounds on OUR reservations. They do NOT cap provider-side billing;
# a provider could charge more and reconcile would surface that as an
# honest overage, never a silent one.
VALIDATION_CAPS_USD = {
    "per_request": Decimal("0.10"),
    "per_provider": Decimal("0.15"),
    "global": Decimal("0.75"),
}

# Per-provider transport policy a Stage-B client must honor. Sync
# providers get a single total budget; OpenAI's documented background
# lifecycle additionally bounds polling.
LIVE_TIMEOUTS: dict[str, dict[str, float]] = {
    "openai":        {"connect_s": 10, "total_s": 300,
                      "poll_deadline_s": 300, "poll_min_interval_s": 1},
    "anthropic":     {"connect_s": 10, "total_s": 60},
    "google-gemini": {"connect_s": 10, "total_s": 60},
    "deepseek":      {"connect_s": 10, "total_s": 60},
    "xai":           {"connect_s": 10, "total_s": 60},
}
MAX_RETRY_AFTER_S = 60  # documented retry hints are capped, never trusted blind


# ------------------------------------------------------------------
# durable state (AppSetting KV — restart-safe, project DB)
# ------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state(session: Session) -> dict[str, Any]:
    row = session.get(AppSetting, LIVE_GATE_KEY)
    st = (row.value_json or {}).get("value") if row else None
    return st or {"global_disabled": False, "global_disabled_at": None,
                  "providers": {}}


def _save(session: Session, st: dict[str, Any]) -> None:
    row = session.get(AppSetting, LIVE_GATE_KEY)
    if row is None:
        row = AppSetting(key=LIVE_GATE_KEY, value_json={})
        session.add(row)
    row.value_json = {"value": st}
    session.flush()


# ------------------------------------------------------------------
# validation helpers
# ------------------------------------------------------------------

def _t1_text_strategy(provider: dict) -> dict | None:
    for s in provider.get("strategies") or []:
        if (s.get("tier") == "T1" and s.get("method") == "official_api"
                and any(c.startswith("text.generate")
                        for c in s.get("capabilities") or [])):
            return s
    return None


def _text_model(provider: dict, model_id: str) -> dict | None:
    for m in provider.get("models") or []:
        if m.get("id") == model_id and m.get("capability") == "text":
            return m
    return None


def _credential_ref_ok(provider: dict, credential_ref: str, vault: Any
                       ) -> tuple[bool, str]:
    """credential_ref must name THIS provider or a same-account sibling,
    and its vault entry must report configured+readable. Metadata only —
    get_fields is never called here."""
    if credential_ref != provider["key"]:
        acct = catalog.account_of(provider["key"])
        siblings = set(catalog.accounts().get(acct or "", ()))
        if credential_ref not in siblings:
            return False, ("credential_ref must be the provider key or a "
                           "same-account sibling — arbitrary references "
                           "are refused")
    st = vault.status(credential_ref)
    if not st.get("configured"):
        return False, f"no credential stored under '{credential_ref}'"
    if not st.get("readable"):
        return False, (f"stored credential under '{credential_ref}' is "
                       "unreadable — re-enter it")
    return True, ""


def _budgets_exist(session: Session) -> bool:
    return session.execute(
        select(func.count()).select_from(SpendBudget)).scalar() > 0


def live_exposure_micro(session: Session, provider_key: str | None = None
                        ) -> int:
    """Open live reservations + settled live charges — our exposure,
    conservative across all time (a validation 'session' never discounts
    history)."""
    open_q = select(func.coalesce(func.sum(
        SpendReservation.reserved_micro), 0)).where(
        SpendReservation.environment == "live",
        SpendReservation.status == "open")
    chg_q = select(func.coalesce(func.sum(
        SpendCharge.micro), 0)).where(SpendCharge.environment == "live")
    if provider_key:
        open_q = open_q.where(SpendReservation.provider_key == provider_key)
        chg_q = chg_q.where(SpendCharge.provider_key == provider_key)
    return (session.execute(open_q).scalar_one()
            + session.execute(chg_q).scalar_one())


# ------------------------------------------------------------------
# enable / disable — mutations (invoked by cloud.live.* commands only)
# ------------------------------------------------------------------

def enable(session: Session, *, provider_key: str, model_id: str,
           credential_ref: str, vault: Any) -> dict[str, Any]:
    """Grant a provider+model live-lane opt-in. Validates everything a
    future dispatch depends on so a granted row means 'ready modulo
    per-request admission', not just a flag."""
    provider = catalog.get_provider(provider_key)
    if provider is None or provider_key not in LIVE_PROVIDERS:
        raise LiveGateRefused(
            "no_contract", f"'{provider_key}' has no documented live "
            "contract — only A-class REST providers can be enabled")
    st = state(session)
    if st.get("global_disabled"):
        raise LiveGateRefused(
            "global_kill", "live execution is globally disabled — lift "
            "the kill switch before enabling providers")
    strat = _t1_text_strategy(provider)
    if strat is None:
        raise LiveGateRefused(
            "no_t1_strategy", f"{provider_key} has no documented T1 "
            "official_api text strategy")
    if record_stale(strat):
        raise LiveGateRefused(
            "evidence_stale", f"{provider_key} strategy evidence is stale "
            "— re-verify catalog evidence before enabling")
    model = _text_model(provider, model_id)
    if model is None:
        raise LiveGateRefused(
            "model_not_granted", f"model '{model_id}' is not a documented "
            f"text model for {provider_key}")
    if not model.get("cost_table"):
        raise LiveGateRefused(
            "cost_unboundable", f"model '{model_id}' has no catalog cost "
            "table — live exposure cannot be bounded, refusing")
    ok, why = _credential_ref_ok(provider, credential_ref, vault)
    if not ok:
        raise LiveGateRefused("credential", why)
    if not _budgets_exist(session):
        raise LiveGateRefused(
            "no_budgets", "no spend budgets configured — live enablement "
            "requires at least one budget bounding exposure")
    entry = {"enabled": True, "model_id": model_id,
             "credential_ref": credential_ref,
             "granted_at": _now(), "granted_by": "owner",
             "revoked_at": None, "revoked_reason": None}
    st["providers"][provider_key] = entry
    _save(session, st)
    return entry


def disable(session: Session, *, provider_key: str,
            reason: str = "user") -> dict[str, Any]:
    st = state(session)
    entry = st["providers"].get(provider_key)
    if entry is None:
        entry = {"enabled": False, "model_id": None, "credential_ref": None,
                 "granted_at": None, "granted_by": None}
    entry.update(enabled=False, revoked_at=_now(), revoked_reason=reason)
    st["providers"][provider_key] = entry
    _save(session, st)
    return entry


def disable_all(session: Session, *, reason: str = "kill_switch"
                ) -> dict[str, Any]:
    """Global kill switch — blocks ALL live dispatch and revokes every
    provider grant. Open reservations are untouched: settlement or
    reconciliation remains possible, resubmission does not happen."""
    st = state(session)
    st["global_disabled"] = True
    st["global_disabled_at"] = _now()
    for entry in st["providers"].values():
        entry.update(enabled=False, revoked_at=_now(),
                     revoked_reason=reason)
    _save(session, st)
    return st


def resume_global(session: Session) -> dict[str, Any]:
    """Lift the global kill only — providers revoked by the kill stay
    disabled and must be re-enabled one by one."""
    st = state(session)
    st["global_disabled"] = False
    _save(session, st)
    return st


# ------------------------------------------------------------------
# dispatch admission — the gate a Stage-B transport must pass
# ------------------------------------------------------------------

def dispatch_admission(session: Session, *, provider_key: str,
                       model_id: str, max_cost_usd: Any,
                       vault: Any) -> tuple[bool, str]:
    """Returns (allowed, code). Every refusal is fail-closed — a 'maybe'
    never reaches a transport. No financial reservation is created here;
    spend.reserve() remains the atomic booking step downstream."""
    st = state(session)
    if st.get("global_disabled"):
        return False, "global_kill"
    entry = st["providers"].get(provider_key) or {}
    if not entry.get("enabled"):
        return False, "not_enabled"
    if entry.get("model_id") != model_id:
        return False, "model_not_granted"
    provider = catalog.get_provider(provider_key)
    strat = _t1_text_strategy(provider or {})
    if strat is None or record_stale(strat):
        return False, "evidence_stale"
    ok, _why = _credential_ref_ok(provider or {}, entry["credential_ref"],
                                  vault)
    if not ok:
        return False, "credential"
    if max_cost_usd is None:
        return False, "unbounded"
    if Decimal(str(max_cost_usd)) > VALIDATION_CAPS_USD["per_request"]:
        return False, "cap_per_request"
    if not _budgets_exist(session):
        return False, "no_budgets"
    prov_exp = live_exposure_micro(session, provider_key)
    if prov_exp + usd_to_micro(max_cost_usd) > usd_to_micro(
            VALIDATION_CAPS_USD["per_provider"]):
        return False, "cap_per_provider"
    glob_exp = live_exposure_micro(session)
    if glob_exp + usd_to_micro(max_cost_usd) > usd_to_micro(
            VALIDATION_CAPS_USD["global"]):
        return False, "cap_global"
    # One in-flight live job per provider: an open reservation means a
    # request is running OR ambiguous — either way nothing new goes out.
    open_n = session.execute(
        select(func.count()).select_from(SpendReservation).where(
            SpendReservation.environment == "live",
            SpendReservation.provider_key == provider_key,
            SpendReservation.status == "open")).scalar_one()
    if open_n:
        return False, "concurrency"
    return True, "ok"


# ------------------------------------------------------------------
# egress allowlist — enforced by a Stage-B transport before any socket
# ------------------------------------------------------------------

def _allowed_host(provider_key: str) -> str | None:
    spec = SPECS.get(provider_key)
    return urlparse(spec.base_url).hostname if spec else None


def egress_allowed(provider_key: str, url: str) -> tuple[bool, str]:
    """Validate a request target: https only, no userinfo, host must be
    the provider's documented base host exactly — subdomains of nothing,
    lookalikes never."""
    u = urlparse(url)
    if u.scheme != "https":
        return False, "scheme"
    if u.username or u.password:
        return False, "userinfo"
    host = _allowed_host(provider_key)
    if host is None or u.hostname != host:
        return False, "host"
    return True, "ok"


def redirect_allowed(provider_key: str, location: str) -> tuple[bool, str]:
    """Redirects are refused unless the target passes the same gate —
    a 3xx can never widen the allowlist."""
    return egress_allowed(provider_key, location)


def retry_after_s(headers: dict[str, str]) -> float | None:
    """Documented Retry-After hint, capped — malformed/absent → None so
    the caller falls back to its own bounded poll cadence."""
    raw = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return min(v, MAX_RETRY_AFTER_S)


# ------------------------------------------------------------------
# verified_remote evidence contract — Stage B only
# ------------------------------------------------------------------

# What a real transport MUST attach to a remote result before
# provenance_confidence() may see provider_verified=True.
#
# B1 provenance correction: a remote_job_id is NOT required — some
# legitimate synchronous provider responses expose no remote operation
# id, and none may be fabricated. What is required instead:
#   authenticated transport + approved endpoint identity +
#   request/job correlation + response integrity evidence + durable
#   audit linkage. remote_job_id is recorded when the provider supplies
#   one, inside correlation only.
VERIFIED_REMOTE_EVIDENCE = {
    "transport": "live",            # never "mock"/"simulated"/"controlled"
    "authenticated": True,          # credential was actually applied
    "environment": "live",
    "endpoint_host": "<allowlisted host the request was sent to>",
    "correlation": {"job_id": "<durable local job>",
                    "command_id": "<audited command or none>",
                    "remote_job_id": "<provider id when documented>"},
    "response_fingerprint": "<sha256 of canonical response body>",
    "evidence_url": "<official API doc URL for the endpoint used>",
    "audit_command_id": "<command row backing this dispatch>",
}


def verification_evidence_valid(ev: dict | None, *,
                                transport_is_live: bool,
                                provider_key: str | None = None) -> bool:
    """Gate for stamping provider_verified. In Stage A transport_is_live
    is ALWAYS False — no live transports exist — so this can never
    return True and verified_remote stays unreachable. In B1 the flag
    is True only when the transport's inner client was the real one —
    injected controlled transports can never produce it."""
    if not transport_is_live:
        return False
    ev = ev or {}
    host = ev.get("endpoint_host")
    corr = ev.get("correlation") or {}
    host_ok = bool(provider_key) and host == _allowed_host(provider_key)
    return (ev.get("transport") == "live"
            and ev.get("authenticated") is True
            and ev.get("environment") == "live"
            and host_ok
            and bool(corr.get("job_id"))
            and bool(ev.get("response_fingerprint"))
            and bool(ev.get("evidence_url")))


# ------------------------------------------------------------------
# read model for the status endpoint / UI
# ------------------------------------------------------------------

def status_report(session: Session, vault: Any) -> dict[str, Any]:
    st = state(session)
    providers: dict[str, Any] = {}
    for key in sorted(LIVE_PROVIDERS):
        entry = st["providers"].get(key) or {}
        allowed, code = dispatch_admission(
            session, provider_key=key,
            model_id=entry.get("model_id") or "",
            max_cost_usd=VALIDATION_CAPS_USD["per_request"], vault=vault)
        providers[key] = {
            **entry,
            "credential_configured":
                bool(entry.get("credential_ref"))
                and vault.status(entry["credential_ref"]).get("configured"),
            "dispatch_ready": allowed,
            "blocked_reason": None if allowed else code,
            "live_exposure_usd":
                micro_to_usd(live_exposure_micro(session, key)),
        }
    return {
        "stage": "A — safety gates only; no live dispatch exists",
        "global_disabled": st.get("global_disabled", False),
        "global_disabled_at": st.get("global_disabled_at"),
        "caps_usd": {k: str(v) for k, v in VALIDATION_CAPS_USD.items()},
        "global_exposure_usd": micro_to_usd(live_exposure_micro(session)),
        "timeouts": LIVE_TIMEOUTS,
        "providers": providers,
    }
