"""Organization, invitation and SSO schemas (Phase C2)."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ---------------------------------------------------------------------------
# Organizations & membership
# ---------------------------------------------------------------------------
class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class OrgUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class OrgOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    created_at: dt.datetime


class OrgMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID
    role: str


class OrgMemberRoleUpdate(BaseModel):
    role: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
class InvitationCreate(BaseModel):
    email: EmailStr
    role: str = Field(default="member", max_length=64)


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    role: str
    expires_at: dt.datetime
    accepted_at: dt.datetime | None
    revoked_at: dt.datetime | None
    created_at: dt.datetime


class InvitationCreatedOut(InvitationOut):
    """Returned exactly once at creation: carries the raw, un-hashed token."""

    token: str


class InvitationAccept(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class InvitationAcceptedOut(BaseModel):
    org_id: uuid.UUID
    user_id: uuid.UUID
    role: str


# ---------------------------------------------------------------------------
# SSO
# ---------------------------------------------------------------------------
class SsoConnectionCreate(BaseModel):
    provider: str = Field(default="oidc", max_length=32)
    issuer: str = Field(min_length=1, max_length=512)
    client_id: str = Field(min_length=1, max_length=255)
    #: Name of the secret-manager entry holding the client secret. The secret
    #: itself is NEVER accepted or stored here.
    client_secret_ref: str = Field(min_length=1, max_length=255)
    authorization_endpoint: str = Field(min_length=1, max_length=1024)
    token_endpoint: str = Field(min_length=1, max_length=1024)
    jwks_uri: str = Field(min_length=1, max_length=1024)
    redirect_uri: str = Field(min_length=1, max_length=1024)
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])
    #: How the client authenticates at the token endpoint. ``client_secret_basic``
    #: is the OAuth 2.0 default and the only method some IdPs accept; the original
    #: implementation could only do ``client_secret_post``.
    token_endpoint_auth_method: str = Field(
        default="client_secret_post",
        pattern="^(client_secret_post|client_secret_basic)$",
    )


class SsoConnectionUpdate(BaseModel):
    client_id: str | None = Field(default=None, max_length=255)
    client_secret_ref: str | None = Field(default=None, max_length=255)
    authorization_endpoint: str | None = Field(default=None, max_length=1024)
    token_endpoint: str | None = Field(default=None, max_length=1024)
    jwks_uri: str | None = Field(default=None, max_length=1024)
    redirect_uri: str | None = Field(default=None, max_length=1024)
    scopes: list[str] | None = None
    token_endpoint_auth_method: str | None = Field(
        default=None, pattern="^(client_secret_post|client_secret_basic)$"
    )
    active: bool | None = None


class SsoConnectionOut(BaseModel):
    """Connection metadata. Deliberately omits anything secret-shaped."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    provider: str
    issuer: str
    client_id: str
    active: bool
    created_at: dt.datetime
    updated_at: dt.datetime
    client_secret_ref: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    jwks_uri: str = ""
    redirect_uri: str = ""
    scopes: list[str] = Field(default_factory=list)


class SsoAuthorizeOut(BaseModel):
    authorization_url: str
    state: str


class SsoCallbackIn(BaseModel):
    state: str = Field(min_length=1, max_length=4096)
    code: str = Field(min_length=1, max_length=4096)


class SsoLoginOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    org_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    provisioned: bool
