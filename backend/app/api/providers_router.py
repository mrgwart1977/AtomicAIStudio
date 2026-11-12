"""C1 — Settings → AI Providers API.

Provider catalog browsing, per-provider config, credential management
(write-only secrets → DPAPI vault), per-task defaults, mock connection
tests, and per-project overrides (via project.update_settings commands
on the frontend — reads live here).

Secrets never flow back through these endpoints: credential reads return
presence metadata only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_project
from app.db.models import AuditEvent
from app.domain.providers import registry
from app.domain.providers.credentials import CredentialVault, VaultError
from app.domain.providers import catalog
from app.schemas.providers import (
    CredentialStatusOut, CredentialUpdate, DefaultUpdate, DefaultsOut,
    ProviderCapabilitiesOut, ProviderConfigUpdate, ProviderDetailOut,
    ProviderSummaryOut, ProviderTestOut,
)

providers_router = APIRouter()


def _vault(request: Request) -> CredentialVault:
    v = getattr(request.app.state, "provider_vault", None)
    if v is None:  # defensive — wired in lifespan; tests may not set it
        v = CredentialVault(request.app.state.settings.data_dir / "credentials.vault")
        request.app.state.provider_vault = v
    return v


# ---------- catalog + config ----------

@providers_router.get("/api/providers", response_model=list[ProviderSummaryOut])
def list_providers(request: Request, db: Session = Depends(get_db),
                   search: str | None = None, category: str | None = None,
                   access_class: str | None = None):
    if category is not None and category not in catalog.CATEGORIES:
        raise HTTPException(422, f"category must be one of {catalog.CATEGORIES}")
    if access_class is not None and access_class not in catalog.ACCESS_CLASSES:
        raise HTTPException(422, f"access_class must be one of {catalog.ACCESS_CLASSES}")
    return registry.list_providers(db, _vault(request), search=search,
                                   category=category, access_class=access_class)


@providers_router.get("/api/providers/defaults", response_model=DefaultsOut)
def get_defaults(db: Session = Depends(get_db)):
    return {"defaults": registry.get_defaults(db)}


@providers_router.put("/api/providers/defaults", response_model=DefaultsOut)
def set_default(body: DefaultUpdate, db: Session = Depends(get_db)):
    try:
        defaults = registry.set_default(db, body.task, body.provider_key)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.commit()
    return {"defaults": defaults}


@providers_router.get("/api/providers/{key}", response_model=ProviderDetailOut)
def provider_detail(key: str, request: Request, db: Session = Depends(get_db)):
    out = registry.provider_detail(db, _vault(request), key)
    if out is None:
        raise HTTPException(404, "provider not found")
    return out


@providers_router.get("/api/providers/{key}/capabilities",
                      response_model=ProviderCapabilitiesOut)
def provider_capabilities(key: str, request: Request, db: Session = Depends(get_db)):
    """C1.5 — per-feature capability report. `catalog_documented` on every
    entry: evidence URLs are not authorization, capability proof, or a
    tested integration. `dispatch` reflects what the bridge can run today
    (T2/T3 user-driven actions; T1 is never claimed operational)."""
    from app.domain import bridge
    p = catalog.get_provider(key)
    if p is None:
        raise HTTPException(404, "provider not found")
    cfg = registry.get_config(db).get(key, {})
    cred = _vault(request).status(key) if p.get("credential_fields") else None
    report = bridge.capability_report(p)
    report["provider_status"] = registry.compute_status(
        p, cred, bool(cfg.get("enabled")))["status"]
    report["enabled"] = bool(cfg.get("enabled"))
    return report


@providers_router.put("/api/providers/{key}/config", response_model=ProviderDetailOut)
def set_provider_config(key: str, body: ProviderConfigUpdate,
                        request: Request, db: Session = Depends(get_db)):
    if catalog.get_provider(key) is None:
        raise HTTPException(404, "provider not found")
    problems = registry.validate_settings(key, body.settings or {})
    if problems:
        raise HTTPException(422, "; ".join(problems))
    patch = {}
    if body.enabled is not None:
        # C1.5 §9 — stale evidence demotes before enable: refuse to arm a
        # strategy whose verification has aged out.
        if body.enabled and registry.strategy_stale(catalog.get_provider(key)):
            raise HTTPException(422, "strategy verification is stale — "
                                     "needs re-check before enabling")
        patch["enabled"] = body.enabled
    if body.settings is not None:
        patch["settings"] = body.settings
    if body.region is not None:
        prov = catalog.get_provider(key)
        if not prov["region_selectable"]:
            raise HTTPException(422, f"{key} does not support region selection")
        patch["region"] = body.region
    old_dir = ((registry.get_config(db).get(key, {}).get("settings") or {})
               .get("import_dir"))
    registry.set_provider_config(db, key, patch)
    new_dir = ((registry.get_config(db).get(key, {}).get("settings") or {})
               .get("import_dir"))
    if old_dir != new_dir:
        # C1.5 ImportWatcher — folder (re)designation is security-relevant:
        # it's the boundary the scanner is allowed inside. Audit the action
        # (app-level row, project_id=None) WITHOUT the absolute path — a
        # folder name answers "was the boundary moved?" without persisting
        # the user's directory layout.
        from pathlib import Path as _P
        db.add(AuditEvent(
            project_id=None, event_type="provider.import_dir_changed",
            payload_json={
                "provider_key": key,
                "action": "designated" if new_dir else "cleared",
                "folder_name": _P(str(new_dir)).name if new_dir else None,
            }))
    db.commit()
    return registry.provider_detail(db, _vault(request), key)


# ---------- credentials (write-only secrets) ----------

@providers_router.get("/api/providers/{key}/credential", response_model=CredentialStatusOut)
def credential_status(key: str, request: Request, db: Session = Depends(get_db)):
    if catalog.get_provider(key) is None:
        raise HTTPException(404, "provider not found")
    return _vault(request).status(key)


@providers_router.put("/api/providers/{key}/credential", response_model=CredentialStatusOut)
def set_credential(key: str, body: CredentialUpdate,
                   request: Request, db: Session = Depends(get_db)):
    prov = catalog.get_provider(key)
    if prov is None:
        raise HTTPException(404, "provider not found")
    declared = {f["name"] for f in prov.get("credential_fields", [])}
    unknown = set(body.fields) - declared
    if unknown:
        raise HTTPException(422, f"unknown credential field(s): {sorted(unknown)}")
    try:
        _vault(request).set_fields(key, body.fields)
    except VaultError as e:
        raise HTTPException(422, str(e)) from e
    return _vault(request).status(key)


@providers_router.delete("/api/providers/{key}/credential", response_model=CredentialStatusOut)
def delete_credential(key: str, request: Request, db: Session = Depends(get_db)):
    if catalog.get_provider(key) is None:
        raise HTTPException(404, "provider not found")
    _vault(request).delete(key)
    return _vault(request).status(key)


# ---------- mock connection test ----------

@providers_router.post("/api/providers/{key}/test", response_model=ProviderTestOut)
def test_connection(key: str, request: Request, db: Session = Depends(get_db)):
    try:
        return registry.test_connection(db, _vault(request), key)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


# NOTE: /api/projects/{project_id}/providers lives in api.routers — it must be
# registered BEFORE the {etype} catch-all there, which would shadow it here.
