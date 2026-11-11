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
from app.domain.providers import registry
from app.domain.providers.credentials import CredentialVault, VaultError
from app.domain.providers import catalog
from app.schemas.providers import (
    CredentialStatusOut, CredentialUpdate, DefaultUpdate, DefaultsOut,
    ProviderConfigUpdate, ProviderDetailOut, ProviderSummaryOut,
    ProviderTestOut,
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
        patch["enabled"] = body.enabled
    if body.settings is not None:
        patch["settings"] = body.settings
    if body.region is not None:
        prov = catalog.get_provider(key)
        if not prov["region_selectable"]:
            raise HTTPException(422, f"{key} does not support region selection")
        patch["region"] = body.region
    registry.set_provider_config(db, key, patch)
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
