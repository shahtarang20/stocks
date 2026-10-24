"""Project CRUD + API key management (account isolation enforced)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user
from app.models.org import ApiKey, OrganizationMember, Project
from app.core.security import generate_api_key
from app.schemas.auth import CurrentUser
from app.schemas.project import (
    ApiKeyCreate,
    ApiKeyCreatedOut,
    ApiKeyOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from app.services.audit import record_audit

router = APIRouter(prefix="/projects", tags=["projects"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


async def _user_org_ids(db: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    result = await db.execute(
        select(OrganizationMember.org_id).where(OrganizationMember.user_id == user_id)
    )
    return set(result.scalars().all())


async def _get_owned_project(
    db: AsyncSession, project_id: uuid.UUID, user: CurrentUser
) -> Project:
    """Fetch a project only if the caller owns it or belongs to its org.

    Returns 404 (not 403) for projects the caller may not access, so the
    existence of another user's project is never revealed.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise _NOT_FOUND
    if project.owner_id == user.id:
        return project
    if project.org_id is not None:
        org_ids = await _user_org_ids(db, user.id)
        if project.org_id in org_ids:
            return project
    raise _NOT_FOUND


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    org_id = body.org_id
    if org_id is not None:
        org_ids = await _user_org_ids(db, current.id)
        if org_id not in org_ids:
            # Can't attach a project to an org you're not a member of.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Not a member of that organization"
            )
    project = Project(
        owner_id=current.id, org_id=org_id, name=body.name,
        description=(body.description or "").strip() or None,
    )
    db.add(project)
    await db.flush()
    return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectOut]:
    org_ids = await _user_org_ids(db, current.id)
    stmt = select(Project).where(Project.owner_id == current.id)
    result = await db.execute(stmt)
    projects = list(result.scalars().all())
    if org_ids:
        org_result = await db.execute(select(Project).where(Project.org_id.in_(org_ids)))
        seen = {p.id for p in projects}
        for p in org_result.scalars().all():
            if p.id not in seen:
                projects.append(p)
    return [ProjectOut.model_validate(p) for p in projects]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    project = await _get_owned_project(db, project_id, current)
    return ProjectOut.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    project = await _get_owned_project(db, project_id, current)
    project.name = body.name
    if "description" in body.model_fields_set:
        project.description = (body.description or "").strip() or None
    await db.flush()
    return ProjectOut.model_validate(project)


@router.delete(
    "/{project_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def delete_project(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    project = await _get_owned_project(db, project_id, current)
    await db.delete(project)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
@router.post(
    "/{project_id}/api-keys",
    response_model=ApiKeyCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    project_id: uuid.UUID,
    body: ApiKeyCreate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyCreatedOut:
    project = await _get_owned_project(db, project_id, current)
    full_key, prefix, key_hash = generate_api_key()
    api_key = ApiKey(project_id=project.id, name=body.name, prefix=prefix, key_hash=key_hash)
    db.add(api_key)
    await db.flush()
    await record_audit(
        db, action="api_key.create", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="api_key", resource_id=str(api_key.id),
    )
    await db.flush()
    base = ApiKeyOut.model_validate(api_key)
    # The full secret is returned exactly once, here.
    return ApiKeyCreatedOut(**base.model_dump(), api_key=full_key)


@router.get("/{project_id}/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKeyOut]:
    project = await _get_owned_project(db, project_id, current)
    result = await db.execute(select(ApiKey).where(ApiKey.project_id == project.id))
    return [ApiKeyOut.model_validate(k) for k in result.scalars().all()]


@router.delete(
    "/{project_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def revoke_api_key(
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    project = await _get_owned_project(db, project_id, current)
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.project_id == project.id)
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    if api_key.revoked_at is None:
        api_key.revoked_at = utcnow()
    await record_audit(
        db, action="api_key.revoke", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="api_key", resource_id=str(api_key.id),
    )
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
