"""Typed API contracts. The generated TypeScript client is produced
from /openapi.json so these are the single source of truth."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------- projects ----------

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    template: str = Field(default="blank", pattern="^(blank|short_film|series|music_video)$")
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectRename(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    id: str
    name: str
    status: str
    template: str
    revision: int
    settings_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------- prompt draft ----------

class PromptDraftOut(BaseModel):
    project_id: str
    text: str
    scope: str
    updated_at: datetime


# ---------- commands ----------

CommandType = Literal[
    "prompt.set_text",
    "project.rename",
    "project.archive",
    "project.restore",
    "generate.image.mock",
]


class CommandPreviewRequest(BaseModel):
    command_type: CommandType
    payload: dict[str, Any] = Field(default_factory=dict)
    scope: str = Field(default="this_take")
    origin: str = Field(default="prompt_bar")
    target_type: str | None = None
    target_id: str | None = None


class CommandCommitRequest(CommandPreviewRequest):
    expected_revision: int
    idempotency_key: str = Field(min_length=8, max_length=80)


class CommandProposalOut(BaseModel):
    command: dict[str, Any]
    summary: str
    affected: list[dict[str, Any]]
    warnings: list[str]
    estimated_cost: dict[str, Any]
    current_revision: int
    launches_generation: bool


class CommandOut(BaseModel):
    id: str
    project_id: str
    origin: str
    command_type: str
    scope: str
    status: str
    payload_json: dict[str, Any]
    created_at: datetime


# ---------- jobs & assets ----------

class JobOut(BaseModel):
    id: str
    project_id: str
    adapter_id: str
    kind: str
    status: str
    progress: float
    message: str
    error: str | None
    resumable: bool
    output_asset_id: str | None
    command_id: str | None
    created_at: datetime
    completed_at: datetime | None


class AssetOut(BaseModel):
    id: str
    project_id: str
    kind: str
    origin: str
    uri: str
    sha256: str
    created_at: datetime


# ---------- adapters / hardware ----------

class AdapterProfileOut(BaseModel):
    adapter_id: str
    display_name: str
    adapter_kind: str
    tasks: list[str]
    supports: dict[str, bool]
    limits: dict[str, Any]
    requires_cloud: bool
    cost_model: str
    notes: str
