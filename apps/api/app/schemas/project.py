"""Project & API key schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    org_id: uuid.UUID | None = None


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    org_id: uuid.UUID | None
    name: str
    description: str | None = None
    created_at: dt.datetime


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ApiKeyOut(BaseModel):
    """API key metadata - never exposes the secret."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    prefix: str
    last_used_at: dt.datetime | None
    created_at: dt.datetime
    revoked_at: dt.datetime | None


class ApiKeyCreatedOut(ApiKeyOut):
    """Returned exactly once at creation time, including the full secret."""

    api_key: str
