"""Organization membership, role and invitation service (Phase C2).

Design notes
------------
* **Org roles are their own namespace.** ``OrganizationMember.role`` is a plain
  ``String(64)`` column; platform roles (``customer``, ``administrator`` …) say
  what a user may do on the *platform*, org roles say what they may do inside
  one tenant. We store ``owner`` / ``admin`` / ``member`` and never mix the two
  vocabularies, so a platform administrator does not silently gain write access
  to every tenant's organization.
* **Non-members get 404, never 403.** Returning 403 would confirm that an
  organization with that id exists, which leaks tenant structure to outsiders.
  Insufficient *in-org* rank is a 403, because a member already knows the org
  exists.
* **Invitation tokens are stored hashed** (SHA-256, like enrollment/verification
  tokens). A database leak must not yield working invitation links.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Final

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_opaque_token, hash_token
from app.db.base import ensure_aware, utcnow
from app.models.enterprise import OrganizationInvitation
from app.models.org import Organization, OrganizationMember
from app.schemas.auth import CurrentUser

ORG_ROLE_OWNER: Final = "owner"
ORG_ROLE_ADMIN: Final = "admin"
ORG_ROLE_MEMBER: Final = "member"

#: Strictly ordered rank: owner > admin > member. Used for both permission
#: checks and "an invitation may promote but never demote" logic.
ORG_ROLE_RANK: Final[dict[str, int]] = {
    ORG_ROLE_MEMBER: 1,
    ORG_ROLE_ADMIN: 2,
    ORG_ROLE_OWNER: 3,
}

#: Invitations are short-lived by design: a leaked mailbox should not grant
#: tenant access months later. 7 days matches common SaaS practice.
INVITATION_TTL_SECONDS: Final = 7 * 24 * 3600

ORG_NOT_FOUND: Final = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
)
_INVALID_INVITATION: Final = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_invitation"
)


def normalize_org_role(role: str) -> str:
    """Validate and normalize an org role string (422 on anything unknown)."""
    candidate = (role or "").strip().lower()
    if candidate not in ORG_ROLE_RANK:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"role must be one of {sorted(ORG_ROLE_RANK)}",
        )
    return candidate


def role_rank(role: str | None) -> int:
    """Rank of a stored role; unknown/missing roles rank below ``member``."""
    if role is None:
        return 0
    return ORG_ROLE_RANK.get(str(getattr(role, "value", role)).lower(), 0)


async def get_membership(
    db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> OrganizationMember | None:
    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.org_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def get_org_for_member(
    db: AsyncSession, org_id: uuid.UUID, user: CurrentUser
) -> tuple[Organization, OrganizationMember]:
    """Load an org the caller belongs to, or raise 404.

    A non-member cannot distinguish "does not exist" from "not yours", which is
    exactly the point.
    """
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise ORG_NOT_FOUND
    membership = await get_membership(db, org_id, user.id)
    if membership is None:
        raise ORG_NOT_FOUND
    return org, membership


async def require_org_role(
    db: AsyncSession, org_id: uuid.UUID, user: CurrentUser, minimum: str
) -> tuple[Organization, OrganizationMember]:
    """Load an org and assert the caller's in-org rank is at least ``minimum``."""
    org, membership = await get_org_for_member(db, org_id, user)
    if role_rank(membership.role) < ORG_ROLE_RANK[minimum]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"requires org role {minimum} or higher",
        )
    return org, membership


async def count_owners(db: AsyncSession, org_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(OrganizationMember)
        .where(
            OrganizationMember.org_id == org_id,
            OrganizationMember.role == ORG_ROLE_OWNER,
        )
    )
    return int(result.scalar_one())


async def assert_not_last_owner(
    db: AsyncSession, membership: OrganizationMember, *, action: str
) -> None:
    """Refuse an operation that would leave an organization ownerless.

    An ownerless org can never be administered again (nobody can promote a new
    owner), so this is enforced for demotion, removal and self-leave alike.
    """
    if role_rank(membership.role) != ORG_ROLE_RANK[ORG_ROLE_OWNER]:
        return
    if await count_owners(db, membership.org_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot {action} the last owner of the organization",
        )


async def create_organization(
    db: AsyncSession, *, name: str, owner: CurrentUser
) -> Organization:
    """Create an org and seat its creator as the first owner."""
    org = Organization(name=name, owner_id=owner.id)
    db.add(org)
    await db.flush()
    db.add(OrganizationMember(org_id=org.id, user_id=owner.id, role=ORG_ROLE_OWNER))
    await db.flush()
    return org


async def list_user_orgs(db: AsyncSession, user_id: uuid.UUID) -> list[Organization]:
    result = await db.execute(
        select(Organization)
        .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(Organization.created_at)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
async def create_invitation(
    db: AsyncSession,
    *,
    org: Organization,
    email: str,
    role: str,
    invited_by: CurrentUser,
) -> tuple[OrganizationInvitation, str]:
    """Issue a single-use, expiring invitation.

    Returns ``(row, raw_token)``; the raw token is shown/emailed exactly once
    and only its SHA-256 hash is persisted.
    """
    normalized_email = email.strip().lower()
    normalized_role = normalize_org_role(role)

    # Supersede any outstanding invitation for the same mailbox so an org never
    # has two live tokens granting different roles.
    outstanding = await db.execute(
        select(OrganizationInvitation).where(
            OrganizationInvitation.org_id == org.id,
            OrganizationInvitation.email == normalized_email,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
        )
    )
    now = utcnow()
    for stale in outstanding.scalars().all():
        stale.revoked_at = now

    raw_token = generate_opaque_token()
    invitation = OrganizationInvitation(
        org_id=org.id,
        email=normalized_email,
        role=normalized_role,
        token_hash=hash_token(raw_token),
        expires_at=now + dt.timedelta(seconds=INVITATION_TTL_SECONDS),
        invited_by_user_id=invited_by.id,
    )
    db.add(invitation)
    await db.flush()
    return invitation, raw_token


async def accept_invitation(
    db: AsyncSession, *, raw_token: str, user: CurrentUser
) -> tuple[Organization, OrganizationMember, OrganizationInvitation]:
    """Redeem an invitation token for the authenticated caller.

    Forged, expired, revoked and already-used tokens are all rejected with the
    same generic error so the endpoint cannot be used as a token oracle.
    Accepting never *demotes* an existing member and never creates a duplicate
    membership row.
    """
    invitation = (
        await db.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.token_hash == hash_token(raw_token or "")
            )
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise _INVALID_INVITATION
    if invitation.accepted_at is not None or invitation.revoked_at is not None:
        raise _INVALID_INVITATION
    expires_at = ensure_aware(invitation.expires_at)
    if expires_at is None or expires_at <= utcnow():
        raise _INVALID_INVITATION
    if invitation.email != user.email.strip().lower():
        # The token is valid but was issued to someone else's mailbox.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invitation_email_mismatch"
        )

    org = (
        await db.execute(
            select(Organization).where(Organization.id == invitation.org_id)
        )
    ).scalar_one_or_none()
    if org is None:
        raise _INVALID_INVITATION

    membership = await get_membership(db, invitation.org_id, user.id)
    if membership is None:
        membership = OrganizationMember(
            org_id=invitation.org_id, user_id=user.id, role=invitation.role
        )
        db.add(membership)
    elif role_rank(invitation.role) > role_rank(membership.role):
        # Promotion is fine (the inviter already had admin authority); silent
        # demotion is not, so a re-invite as `member` leaves an admin alone.
        membership.role = invitation.role

    invitation.accepted_at = utcnow()
    invitation.accepted_by_user_id = user.id
    await db.flush()
    return org, membership, invitation
