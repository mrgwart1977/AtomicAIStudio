"""C1 provider API schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ProviderSummaryOut(BaseModel):
    key: str
    label: str
    category: str
    account: str
    status: str
    status_label: str
    tier: str
    method: str
    access_class: str
    enabled: bool
    credential_configured: bool
    credential_unreadable: bool
    capabilities: list[str]
    is_mock_only: bool
    status_reason: str


class CredentialStatusOut(BaseModel):
    configured: bool
    fields: list[str]
    set_at: str | None
    readable: bool
    unreadable_reason: str | None


class ProviderDetailOut(BaseModel):
    key: str
    label: str
    category: str
    access_class: str
    account: str
    auth_methods: list[str]
    credential_fields: list[dict[str, Any]]
    base_url: str | None
    region_selectable: bool
    job_model: str | None
    pricing_model: str | None
    status_reason: str
    strategies: list[dict[str, Any]]
    models: list[dict[str, Any]]
    docs_url: str | None
    tos_url: str | None
    privacy_summary: str
    notes: str
    aliases: list[str]
    shared_credential_with: list[str]
    enabled: bool
    settings: dict[str, Any]
    region: str | None
    credential: CredentialStatusOut
    status: str
    status_label: str
    tier: str
    method: str
    capabilities: list[str]
    is_mock_only: bool


class ProviderConfigUpdate(BaseModel):
    enabled: bool | None = None
    settings: dict[str, Any] | None = None
    region: str | None = None


class CredentialUpdate(BaseModel):
    fields: dict[str, str]


class ProviderTestOut(BaseModel):
    ok: bool
    via: str
    note: str | None = None
    error: str | None = None
    would_use: str | None = None
    tier: str | None = None


class DefaultsOut(BaseModel):
    defaults: dict[str, str]


class DefaultUpdate(BaseModel):
    task: str
    provider_key: str | None
