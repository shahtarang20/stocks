"""Enterprise models (Phase C): org invitations, SSO connections and Stripe
Connect payout accounts.

Token note: like ``enrollment_tokens``, only the SHA-256 ``token_hash`` is
persisted. A database leak must not hand the attacker working invitation links.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow
from app.models.enums import Role

SSO_PROVIDERS = ("oidc", "saml")
CONNECT_ACCOUNT_STATUSES = ("pending", "active", "restricted", "disabled")


class OrganizationInvitation(Base):
    """A pending invitation to join an organization with a given role."""

    __tablename__ = "organization_invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_org_invitations_token_hash"),
        Index("ix_org_invitations_org_email", "org_id", "email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False, default=Role.customer.value)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SsoConnection(Base):
    """An org's federated identity provider configuration.

    ``config`` holds non-secret endpoint metadata (authorize/token/JWKS URLs,
    scopes, attribute mapping). Client secrets belong in the secret store, not
    in this JSON column.
    """

    __tablename__ = "sso_connections"
    __table_args__ = (
        UniqueConstraint("org_id", "provider", "issuer", name="uq_sso_connections_org_provider_issuer"),
        Index("ix_sso_connections_issuer_active", "issuer", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # oidc | saml
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ConnectAccount(Base):
    """A provider's Stripe Connect account used for real-world payouts.

    One per provider (UNIQUE): payouts must never fan out to several accounts.
    ``payouts_enabled`` mirrors Stripe's capability flag and gates withdrawal.
    """

    __tablename__ = "connect_accounts"
    __table_args__ = (
        UniqueConstraint("provider_id", name="uq_connect_accounts_provider_id"),
        UniqueConstraint("stripe_account_id", name="uq_connect_accounts_stripe_account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    stripe_account_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # pending | active | restricted | disabled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    payouts_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details_submitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requirements: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
