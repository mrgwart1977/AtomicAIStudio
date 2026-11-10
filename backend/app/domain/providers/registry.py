"""C1 provider registry — catalog + per-user config + honest status.

Config (non-secret) lives in app_settings under keys:
  providers.config   — {provider_key: {enabled, settings{}, region}}
  providers.defaults — {task: provider_key}
Secrets live in the CredentialVault — never here.

Status semantics (never conflate):
  verified       — A/B class with API path + required credential present & readable
  needs_key      — API path exists but no credential configured
  plan_required  — B class (plan/entitlement gate) with no credential
  preview_only   — API exists but preview/limited (Canva, Meta)
  assisted       — best path is T2/T3 (no API) — workflow, not integration
  external_only  — T4 informational (Udio)
  mock           — connection test used the mock connector (never "connected")
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AppSetting
from app.domain.providers import catalog
from app.domain.providers.credentials import CredentialVault

log = logging.getLogger(__name__)

CFG_KEY = "providers.config"
DEFAULTS_KEY = "providers.defaults"
TASKS = ("text", "image", "video", "audio")

# Strategies whose method is a direct programmatic channel.
_T1_METHODS = {"official_api", "aggregator_api", "mcp_connector", "desktop_plugin"}


# ---------- config persistence ----------

def _get_kv(session: Session, key: str, default):
    row = session.get(AppSetting, key)
    if row is None:
        return default
    return (row.value_json or {}).get("value", default)


def _set_kv(session: Session, key: str, value) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value_json={})
        session.add(row)
        session.flush()
    row.value_json = {"value": value}


def get_config(session: Session) -> dict[str, Any]:
    return _get_kv(session, CFG_KEY, {})


def set_provider_config(session: Session, key: str, patch: dict[str, Any]) -> dict:
    """Merge {enabled, settings, region} patch for one provider."""
    cfg = get_config(session)
    cur = cfg.get(key, {})
    merged = {**cur}
    for k in ("enabled", "settings", "region"):
        if k in patch:
            merged[k] = patch[k]
    merged.setdefault("enabled", False)
    cfg[key] = merged
    _set_kv(session, CFG_KEY, cfg)
    return merged


def get_defaults(session: Session) -> dict[str, str]:
    return _get_kv(session, DEFAULTS_KEY, {})


def set_default(session: Session, task: str, provider_key: str | None) -> dict:
    if task not in TASKS:
        raise ValueError(f"unknown task '{task}'")
    if provider_key is not None:
        prov = catalog.get_provider(provider_key)
        if prov is None:
            raise ValueError(f"unknown provider '{provider_key}'")
        if prov["category"] != task:
            raise ValueError(f"{provider_key} is a {prov['category']} provider, not {task}")
    defaults = get_defaults(session)
    if provider_key is None:
        defaults.pop(task, None)
    else:
        defaults[task] = provider_key
    _set_kv(session, DEFAULTS_KEY, defaults)
    return defaults


# ---------- status ----------

def best_strategy(provider: dict) -> dict | None:
    """Preferred strategy: first T1 wins (T1 = direct programmatic); among
    non-T1 providers the catalog's declared order is authoritative — it
    encodes the recommended path (e.g. QuillBot → T3 clipboard round-trip)."""
    strats = provider.get("strategies") or []
    if not strats:
        return None
    for s in strats:
        if s["tier"] == "T1":
            return s
    return strats[0]


def credential_required(provider: dict) -> bool:
    return any(f.get("required") for f in provider.get("credential_fields", []))


def compute_status(provider: dict, cred_status: dict | None,
                   enabled: bool) -> dict[str, Any]:
    """Honest status — access class + credential + tier, never inflated."""
    cls = provider["access_class"]
    strat = best_strategy(provider)
    tier = strat["tier"] if strat else "T4"
    method = strat["method"] if strat else "external_info"
    cred_ok = cred_status.get("configured") and cred_status.get("readable") if cred_status else False
    cred_missing_readable = bool(cred_status and cred_status.get("configured")
                                 and not cred_status.get("readable"))

    if cred_missing_readable:
        base, label = "credential_unreadable", "Credential unreadable — re-enter"
    elif tier == "T4":
        base, label = "external_only", "External only"
    elif tier in ("T2", "T3"):
        base, label = ("assisted" if tier == "T2" else "manual",
                       "Assisted workflow" if tier == "T2" else "Manual import")
    else:  # T1
        if credential_required(provider) and not cred_ok:
            base = "plan_required" if cls in ("B", "C") else "needs_key"
            label = ("Plan/entitlement required" if base == "plan_required"
                     else "Credential required")
        elif provider["key"] in ("canva", "meta-ai"):
            base, label = "preview_only", "Preview API — verify onboarding"
        elif not enabled:
            base, label = "available", "Ready — enable to use"
        else:
            base, label = "configured", "Configured (credentials stored)"
    return {
        "status": base, "status_label": label, "tier": tier, "method": method,
        "access_class": cls, "enabled": enabled,
        "credential_configured": bool(cred_ok),
        "credential_unreadable": cred_missing_readable,
        "capabilities": (strat or {}).get("capabilities", []),
        "is_mock_only": False,
    }


# ---------- listing / detail ----------

def list_providers(session: Session, vault: CredentialVault, *,
                   search: str | None = None, category: str | None = None,
                   access_class: str | None = None,
                   project_overrides: dict | None = None) -> list[dict]:
    cfg = get_config(session)
    out = []
    for p in catalog.all_providers():
        if category and p["category"] != category:
            continue
        if access_class and p["access_class"] != access_class:
            continue
        if search:
            q = search.lower()
            if q not in p["label"].lower() and q not in p["key"] \
                    and not any(q in a for a in p.get("aliases", [])):
                continue
        c = dict(cfg.get(p["key"], {}))
        overridden = False
        if project_overrides and p["key"] in project_overrides:
            ov = project_overrides[p["key"]] or {}
            c = {**c, **{k: v for k, v in ov.items() if k != "settings"},
                 "settings": {**(c.get("settings") or {}),
                              **(ov.get("settings") or {})}}
            overridden = True
        cred = vault.status(p["key"]) if p.get("credential_fields") else None
        out.append({
            "key": p["key"], "label": p["label"], "category": p["category"],
            "account": p["account"],
            **compute_status(p, cred, bool(c.get("enabled"))),
            "status_reason": p.get("status_reason") or "",
            "overridden": overridden,
            "settings": c.get("settings") or {},
        })
    return out


def provider_detail(session: Session, vault: CredentialVault,
                    key: str) -> dict | None:
    p = catalog.get_provider(key)
    if p is None:
        return None
    cfg = get_config(session).get(key, {})
    cred = vault.status(key) if p.get("credential_fields") else None
    siblings = [k for k in catalog.accounts().get(p["account"], []) if k != key]
    return {
        **{k: p[k] for k in (
            "key", "label", "category", "access_class", "account",
            "auth_methods", "credential_fields", "base_url",
            "region_selectable", "job_model", "pricing_model",
            "status_reason", "strategies", "models", "docs_url", "tos_url",
            "privacy_summary", "notes", "aliases")},
        "shared_credential_with": siblings,
        "enabled": bool(cfg.get("enabled")),
        "settings": cfg.get("settings") or {},
        "region": cfg.get("region"),
        "credential": cred or {"configured": False, "fields": [],
                               "set_at": None, "readable": False,
                               "unreadable_reason": None},
        **compute_status(p, cred, bool(cfg.get("enabled"))),
    }


# ---------- validation ----------

_ALLOWED_SETTING_KEYS = {
    "model", "default_model", "rate_limit_rpm", "timeout_s", "max_retries",
    "region", "prompt_template", "import_dir", "default_asset_type",
    "import_behavior", "project_destination", "provenance", "preferred_site",
    "notes",
}


def validate_settings(key: str, settings: dict) -> list[str]:
    """Reject settings a provider doesn't genuinely support. Returns
    a list of problems (empty = valid)."""
    p = catalog.get_provider(key)
    if p is None:
        return [f"unknown provider '{key}'"]
    problems = []
    for k, v in (settings or {}).items():
        if k not in _ALLOWED_SETTING_KEYS:
            problems.append(f"unsupported setting '{k}'")
            continue
        if k == "model" and p["models"]:
            ids = {m["id"] for m in p["models"]}
            if v not in ids:
                problems.append(f"unknown model '{v}' for {key}")
        if k in ("rate_limit_rpm", "max_retries") and not isinstance(v, int):
            problems.append(f"'{k}' must be an integer")
        if k == "timeout_s" and not isinstance(v, (int, float)):
            problems.append("'timeout_s' must be a number")
        if k == "region" and not p["region_selectable"]:
            problems.append(f"{key} does not support region selection")
    return problems


# ---------- mock connection test ----------

def test_connection(session: Session, vault: CredentialVault, key: str) -> dict:
    """MOCK connector — verifies our own plumbing (credential present,
    vault readable, config valid). It never contacts a real provider and
    never reports 'connected'."""
    p = catalog.get_provider(key)
    if p is None:
        raise ValueError(f"unknown provider '{key}'")
    if credential_required(p):
        try:
            cred = vault.status(key)
        except Exception as e:
            return {"ok": False, "via": "mock", "error": f"vault error: {e}"}
        if not cred["configured"]:
            return {"ok": False, "via": "mock",
                    "error": "no credential configured"}
        if not cred["readable"]:
            return {"ok": False, "via": "mock", "error": "credential unreadable"}
    strat = best_strategy(p)
    return {
        "ok": True, "via": "mock",
        "note": "Mock check: credential + config valid. No live provider "
                "contact — real connectivity is verified per-phase later.",
        "would_use": (strat or {}).get("method"), "tier": (strat or {}).get("tier"),
    }


# ---------- per-project overrides ----------

def project_overrides(project) -> dict:
    return (project.settings_json or {}).get("providers", {})


def effective_for_project(session: Session, key: str, project) -> dict:
    """Effective config = app config ⊕ project override (provider block
    under settings_json['providers'][key])."""
    base = get_config(session).get(key, {})
    ov = project_overrides(project).get(key, {}) if project else {}
    return {**base, **{k: v for k, v in ov.items() if v is not None}}
