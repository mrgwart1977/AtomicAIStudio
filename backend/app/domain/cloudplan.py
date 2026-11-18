"""A7 — provider-neutral cloud job planning.

One read-only resolver composes what an approval needs to see before a
cloud job is submitted:

- provider capability + availability (catalog/registry status —
  verified/needs_key/plan_required/unreadable, never inflated)
- credential PRESENCE only (vault.status metadata — secret values are
  never read here or anywhere in the planning path)
- model/task compatibility (the provider's declared tasks/strategies)
- estimated cost (est_cost_usd from the request — the same number the
  spend reservation would hold; mock providers stay "simulated")
- budget headroom per scope (spend.summary consumption vs limits)
- the environment the job would book against — derived from the
  registered adapter's kind ('mock' → 'simulated'), never from input

The returned dict carries ``plan_fingerprint`` — a sha of the planning
inputs. A submit command that pins it is refused when the plan no
longer matches (provider disabled, credential removed, availability
changed): stale approvals can never silently dispatch.

Live dispatch stays behind ``live_gate.dispatch_admission`` — this
module plans; it never enables, never transports, and mocks never
leave the simulated environment.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from app.domain import spend as _spend
from app.domain.providers import catalog as _catalog
from app.domain.providers import registry as _preg


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True,
                   separators=(",", ":"), default=str).encode()
    ).hexdigest()


# Plan task → the cloud submit command whose registered adapter would
# execute the work. The command table stays in commands.py; the caller
# passes ``adapter_for_command`` so the planner resolves the booking
# environment from the adapter actually wired for the route — the same
# adapter the submit path would use. A task with no route cannot
# dispatch at all: it plans as "unrouted" and can never admit.
TASK_ROUTES = {
    "text": "generate.text.cloud",
    "image": "generate.image.cloud",
    "image.img2img": "generate.image.img2img",
    "image.edit": "generate.image.edit",
}


def _env_for_adapter(adapter) -> str | None:
    """Booking environment from the executing adapter's own transport
    truth — ``spend_environment`` when it can attest it (mocked
    transports book 'simulated'), else adapter_kind ('mock' →
    'simulated'). Anything else is 'live' planning — still gated by
    live_gate at actual submit."""
    if adapter is None:
        return None
    env = getattr(adapter, "spend_environment", None)
    if env:
        return env
    prof = getattr(adapter, "profile", None)
    return ("simulated"
            if getattr(prof, "adapter_kind", "") == "mock"
            else "live")


def resolved_plan(session: Session, project, *, provider_key: str,
                  task: str, model_id: str | None = None,
                  est_cost_usd: Any = None, max_cost_usd: Any = None,
                  vault=None, registry=None,
                  adapter_for_command=None,
                  project_overrides: dict | None = None) -> dict:
    """Compose the approval preview. Read-only — safe to compute at
    preview and again at commit for the staleness check."""
    provider = _catalog.get_provider(provider_key)
    if provider is None:
        return {"ok": False, "error": "unknown_provider",
                "detail": f"provider '{provider_key}' is not in the catalog"}

    overrides = (project.settings_json or {}).get("providers", {}) \
        if project else {}
    if project_overrides:
        overrides = project_overrides
    listed = _preg.list_providers(session, vault or _NullVault(),
                                  project_overrides=overrides)
    entry = next((p for p in listed if p["key"] == provider_key), None)
    status = (entry or {}).get("status") or "unavailable"
    status_label = (entry or {}).get("status_label") or status
    enabled = bool((entry or {}).get("enabled"))

    # Capability match — the provider's declared task surface, never
    # inferred from success.
    tasks = list(provider.get("tasks") or [])
    strategies = _preg.usable_strategies(provider)
    task_ok = not tasks or task in tasks
    strategy = next((s for s in strategies
                     if task in (s.get("capabilities") or [])
                     or not s.get("capabilities")), None)

    # Credential presence — metadata only.
    cred_required = _preg.credential_required(provider)
    cred_status = None
    if vault is not None and provider.get("credential_fields"):
        cs = vault.status(provider_key)
        cred_status = {"configured": bool(cs.get("configured")),
                       "fields": list(cs.get("fields") or []),
                       "unreadable": bool(cs.get("unreadable"))}

    # Execution route: which registered adapter would run this task —
    # and therefore which environment the reservation would book in.
    route_command = TASK_ROUTES.get(task)
    route_adapter = (adapter_for_command(route_command)
                     if route_command and adapter_for_command
                     else None)
    env = _env_for_adapter(route_adapter)
    # Cost + budget headroom (the environment the job would book in;
    # unrouted tasks still show configured limits for context).
    env_book = env if env in ("simulated", "live") else "simulated"
    est_micro = _spend.usd_to_micro(est_cost_usd) \
        if est_cost_usd is not None else 0
    cap_micro = _spend.usd_to_micro(max_cost_usd) \
        if max_cost_usd is not None else est_micro
    held_micro = min(est_micro, cap_micro) if cap_micro else est_micro
    summary = _spend.summary(session, project_id=project.id,
                             environment=env_book) if project else None
    budgets = []
    over_budget = []
    if summary:
        for row in summary["scopes"]:
            limit = row["limit_micro"]
            consumed = row["consumed_micro"]
            fits = limit is None or (consumed + held_micro) <= limit
            budgets.append({"scope": row["scope"],
                            "limit_micro": limit,
                            "consumed_micro": consumed,
                            "would_exceed": not fits})
            if not fits:
                over_budget.append(row["scope"])

    # Availability — honest states, never inflated.
    availability = "available"
    unavailable_reason = None
    if not enabled:
        availability, unavailable_reason = (
            "disabled", "provider is not enabled for this project")
    elif status in ("needs_key", "credential_unreadable",
                    "plan_required"):
        availability = "blocked"
        unavailable_reason = status_label
    elif not task_ok:
        availability = "incompatible"
        unavailable_reason = (f"provider '{provider_key}' does not "
                              f"declare task '{task}'")
    elif route_adapter is None:
        availability = "unrouted"
        unavailable_reason = (
            f"no execution route for task '{task}' — nothing could "
            "dispatch even if approved")
    elif over_budget:
        availability = "over_budget"
        unavailable_reason = (
            f"estimated reservation exceeds the "
            f"{over_budget[0]} budget")

    would_admit = availability == "available" and enabled
    resolved = {
        "ok": True,
        "provider_key": provider_key,
        "provider_label": provider.get("label"),
        "access_class": provider.get("access_class"),
        "task": task,
        "model_id": model_id,
        "task_supported": task_ok,
        "strategy": ({"key": strategy.get("key"),
                      "capabilities": strategy.get("capabilities")}
                     if strategy else None),
        "status": status,
        "status_label": status_label,
        "enabled": enabled,
        "credential_required": cred_required,
        "credential": cred_status,
        "environment": env or "unrouted",
        "route_command": route_command,
        "routed": route_adapter is not None,
        "cost_model": provider.get("cost_model"),
        "est_cost_usd": est_cost_usd,
        "max_cost_usd": max_cost_usd,
        "reserved_micro": held_micro,
        "budgets": budgets,
        "over_budget": over_budget,
        "availability": availability,
        "unavailable_reason": unavailable_reason,
        "would_admit": would_admit,
        "simulated": env == "simulated",
        "note": ("Planning only — no dispatch, no network, no "
                 "credential values read."),
    }
    resolved["plan_fingerprint"] = _sha({
        "provider_key": provider_key, "task": task,
        "model_id": model_id,
        "est_cost_usd": est_cost_usd,
        "max_cost_usd": max_cost_usd,
        "status": status, "enabled": enabled,
        "credential": cred_status,
        "availability": availability,
        "environment": env or "unrouted",
        "routed": route_adapter is not None,
    })
    return resolved


class _NullVault:
    """Preview may run without a vault (tests) — presence reads as
    unconfigured rather than crashing or lying."""

    def status(self, key: str) -> dict:
        return {"configured": False, "fields": [], "unreadable": False}
