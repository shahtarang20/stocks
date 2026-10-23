"""Organizations: membership, roles, invitations and shared projects (Phase C2).

Access rules enforced here:
* every org-scoped route requires membership, and non-members get **404** so
  org ids cannot be enumerated;
* mutations require ``admin`` or ``owner`` rank inside the org — platform roles
  grant nothing here;
* a member can never raise their own rank, and the last owner can never be
  demoted, removed or allowed to leave.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user
from app.models.enterprise import OrganizationInvitation
from app.models.org import OrganizationMember, Project
from app.schemas.auth import CurrentUser
from app.schemas.orgs import (
    InvitationAccept,
    InvitationAcceptedOut,
    InvitationCreate,
    InvitationCreatedOut,
    InvitationOut,
    OrgCreate,
    OrgMemberOut,
    OrgMemberRoleUpdate,
    OrgOut,
    OrgUpdate,
)
from app.schemas.project import ProjectOut
from app.services.audit import record_audit
from app.services.email import send_email
from app.services.orgs import (
    ORG_ROLE_ADMIN,
    ORG_ROLE_MEMBER,
    ORG_ROLE_OWNER,
    accept_invitation,
    assert_not_last_owner,
    count_owners,
    create_invitation,
    create_organization,
    get_membership,
    get_org_for_member,
    list_user_orgs,
    normalize_org_role,
    require_org_role,
    role_rank,
)

router = APIRouter(prefix="/orgs", tags=["organizations"])

_MEMBER_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Member not found"
)
_INVITE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found"
)


# ---------------------------------------------------------------------------
# Org lifecycle
# ---------------------------------------------------------------------------
@router.post("", response_model=OrgOut, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: OrgCreate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgOut:
    org = await create_organization(db, name=body.name, owner=current)
    await record_audit(
        db, action="org.create", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="organization", resource_id=str(org.id),
    )
    await db.flush()
    return OrgOut.model_validate(org)


@router.get("", response_model=list[OrgOut])
async def list_orgs(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[OrgOut]:
    return [OrgOut.model_validate(o) for o in await list_user_orgs(db, current.id)]


@router.get("/{org_id}", response_model=OrgOut)
async def get_org(
    org_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgOut:
    org, _ = await get_org_for_member(db, org_id, current)
    return OrgOut.model_validate(org)


@router.patch("/{org_id}", response_model=OrgOut)
async def update_org(
    org_id: uuid.UUID,
    body: OrgUpdate,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgOut:
    org, _ = await require_org_role(db, org_id, current, ORG_ROLE_ADMIN)
    org.name = body.name
    await db.flush()
    return OrgOut.model_validate(org)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------
@router.get("/{org_id}/members", response_model=list[OrgMemberOut])
async def list_members(
    org_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[OrgMemberOut]:
    await get_org_for_member(db, org_id, current)
    result = await db.execute(
        select(OrganizationMember).where(OrganizationMember.org_id == org_id)
    )
    return [OrgMemberOut.model_validate(m) for m in result.scalars().all()]


@router.patch("/{org_id}/members/{user_id}", response_model=OrgMemberOut)
async def change_member_role(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    body: OrgMemberRoleUpdate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgMemberOut:
    """Change a member's org role.

    Only owner/admin may call this (a plain member gets 403, which is what stops
    self-escalation), an admin may not mint an owner, and the last owner cannot
    be demoted.
    """
    _, actor = await require_org_role(db, org_id, current, ORG_ROLE_ADMIN)
    target = await get_membership(db, org_id, user_id)
    if target is None:
        raise _MEMBER_NOT_FOUND

    new_role = normalize_org_role(body.role)
    actor_rank = role_rank(actor.role)
    if role_rank(new_role) > actor_rank:
        # Nobody may grant a rank above their own (an admin cannot create owners).
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="cannot grant a role above your own"
        )
    if role_rank(target.role) > actor_rank:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="cannot modify a higher-ranked member"
        )
    if role_rank(new_role) < role_rank(target.role):
        await assert_not_last_owner(db, target, action="demote")

    target.role = new_role
    await record_audit(
        db, action="org.member.role_change", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="organization_member", resource_id=str(target.id),
        reason=new_role,
    )
    await db.flush()
    return OrgMemberOut.model_validate(target)


@router.delete(
    "/{org_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def remove_member(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _, actor = await require_org_role(db, org_id, current, ORG_ROLE_ADMIN)
    target = await get_membership(db, org_id, user_id)
    if target is None:
        raise _MEMBER_NOT_FOUND
    if target.user_id != current.id and role_rank(target.role) > role_rank(actor.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="cannot remove a higher-ranked member"
        )
    await assert_not_last_owner(db, target, action="remove")
    await db.delete(target)
    await record_audit(
        db, action="org.member.remove", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="organization_member", resource_id=str(target.id),
    )
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{org_id}/leave", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def leave_org(
    org_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _, membership = await get_org_for_member(db, org_id, current)
    await assert_not_last_owner(db, membership, action="remove")
    await db.delete(membership)
    await record_audit(
        db, action="org.member.leave", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="organization", resource_id=str(org_id),
    )
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
@router.post(
    "/{org_id}/invitations",
    response_model=InvitationCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    org_id: uuid.UUID,
    body: InvitationCreate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InvitationCreatedOut:
    """Invite an email address into the org; the raw token is returned once."""
    org, actor = await require_org_role(db, org_id, current, ORG_ROLE_ADMIN)
    role = normalize_org_role(body.role)
    if role_rank(role) > role_rank(actor.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="cannot invite above your own role"
        )
    invitation, raw_token = await create_invitation(
        db, org=org, email=str(body.email), role=role, invited_by=current
    )
    accept_url = f"{settings.FRONTEND_URL.rstrip('/')}/orgs/accept-invite?token={raw_token}"
    await send_email(
        to_email=invitation.email,
        subject=f"You've been invited to {org.name} on ComputeCoin",
        html_content=(
            f"<p>{current.email} invited you to join <strong>{org.name}</strong> "
            f"as {role}.</p><p><a href=\"{accept_url}\">Accept the invitation</a></p>"
        ),
        text_content=(
            f"{current.email} invited you to join {org.name} as {role}.\n\n{accept_url}\n"
        ),
    )
    await record_audit(
        db, action="org.invitation.create", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="organization_invitation",
        resource_id=str(invitation.id), reason=role,
    )
    await db.flush()
    base = InvitationOut.model_validate(invitation)
    return InvitationCreatedOut(**base.model_dump(), token=raw_token)


@router.get("/{org_id}/invitations", response_model=list[InvitationOut])
async def list_invitations(
    org_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[InvitationOut]:
    await require_org_role(db, org_id, current, ORG_ROLE_ADMIN)
    result = await db.execute(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.org_id == org_id)
        .order_by(OrganizationInvitation.created_at)
    )
    # The token hash is never serialized: InvitationOut has no such field.
    return [InvitationOut.model_validate(i) for i in result.scalars().all()]


@router.delete(
    "/{org_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def revoke_invitation(
    org_id: uuid.UUID,
    invitation_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_org_role(db, org_id, current, ORG_ROLE_ADMIN)
    invitation = (
        await db.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.id == invitation_id,
                OrganizationInvitation.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise _INVITE_NOT_FOUND
    if invitation.revoked_at is None and invitation.accepted_at is None:
        invitation.revoked_at = utcnow()
    await record_audit(
        db, action="org.invitation.revoke", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="organization_invitation",
        resource_id=str(invitation.id),
    )
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/invitations/accept", response_model=InvitationAcceptedOut)
async def accept_org_invitation(
    body: InvitationAccept,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InvitationAcceptedOut:
    """Redeem an invitation token (single-use, expiring, bound to the mailbox)."""
    org, membership, invitation = await accept_invitation(
        db, raw_token=body.token, user=current
    )
    await record_audit(
        db, action="org.invitation.accept", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="organization", resource_id=str(org.id),
    )
    await db.flush()
    return InvitationAcceptedOut(
        org_id=org.id, user_id=current.id, role=str(membership.role)
    )


# ---------------------------------------------------------------------------
# Shared projects
# ---------------------------------------------------------------------------
@router.get("/{org_id}/projects", response_model=list[ProjectOut])
async def list_org_projects(
    org_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectOut]:
    """Projects shared with the organization; non-members get 404 above."""
    await get_org_for_member(db, org_id, current)
    result = await db.execute(
        select(Project).where(Project.org_id == org_id).order_by(Project.created_at)
    )
    return [ProjectOut.model_validate(p) for p in result.scalars().all()]


@router.get("/{org_id}/owners/count", include_in_schema=False)
async def owner_count(
    org_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Small helper used by the console's 'last owner' warning banner."""
    await get_org_for_member(db, org_id, current)
    return {"owners": await count_owners(db, org_id)}


__all__ = ["router", "ORG_ROLE_OWNER", "ORG_ROLE_ADMIN", "ORG_ROLE_MEMBER"]
