"""Enterprise SSO endpoints: connection management + OIDC login (Phase C2).

The whole router is gated by ``settings.SSO_ENABLED`` and returns 404 when the
feature is off, so a deployment that has not rolled SSO out exposes no surface.
Connection management is org owner/admin only; the login endpoints are
deliberately unauthenticated (they *are* the login).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
import secrets

from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user
from app.models.enterprise import SSO_PROVIDERS, SsoConnection
from app.models.enums import Role
from app.models.org import OrganizationMember
from app.models.user import User, UserRole
from app.schemas.auth import CurrentUser
from app.schemas.orgs import (
    SsoAuthorizeOut,
    SsoCallbackIn,
    SsoConnectionCreate,
    SsoConnectionOut,
    SsoConnectionUpdate,
    SsoLoginOut,
)
from app.services.audit import record_audit
from app.services.auth import get_user_by_email, get_user_roles, issue_session
from app.services.orgs import (
    ORG_ROLE_ADMIN,
    ORG_ROLE_MEMBER,
    get_membership,
    require_org_role,
)
from app.services.sso import (
    OidcClient,
    SecretResolver,
    SsoError,
    assert_public_http_url,
    build_authorization_url,
    extract_email,
    fingerprint_secret,
    get_oidc_client,
    get_secret_resolver,
    issue_state,
    resolve_client_secret,
    verify_id_token,
    verify_state,
)

router = APIRouter(prefix="/sso", tags=["sso"])

# Platform roles that an IdP assertion may never resolve to. A federated login
# is only ever as trustworthy as the IdP, and the IdP is customer-configured.
_PRIVILEGED_ROLES = frozenset({"administrator"})

_CONNECTION_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="SSO connection not found"
)


def _require_sso_enabled() -> None:
    """404 (not 403) when the feature is off: an unused feature is invisible."""
    if not settings.SSO_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _to_out(connection: SsoConnection) -> SsoConnectionOut:
    """Serialize a connection, exposing endpoint metadata but never secrets."""
    config: dict[str, Any] = dict(connection.config or {})
    return SsoConnectionOut(
        id=connection.id,
        org_id=connection.org_id,
        provider=connection.provider,
        issuer=connection.issuer,
        client_id=connection.client_id,
        active=connection.active,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        client_secret_ref=str(config.get("client_secret_ref") or ""),
        authorization_endpoint=str(config.get("authorization_endpoint") or ""),
        token_endpoint=str(config.get("token_endpoint") or ""),
        jwks_uri=str(config.get("jwks_uri") or ""),
        redirect_uri=str(config.get("redirect_uri") or ""),
        scopes=list(config.get("scopes") or []),
    )


# ---------------------------------------------------------------------------
# Connection management (org owner/admin only)
# ---------------------------------------------------------------------------
@router.post(
    "/orgs/{org_id}/connections",
    response_model=SsoConnectionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    org_id: uuid.UUID,
    body: SsoConnectionCreate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    resolver: SecretResolver = Depends(get_secret_resolver),
) -> SsoConnectionOut:
    _require_sso_enabled()
    await require_org_role(db, org_id, current, ORG_ROLE_ADMIN)
    provider = body.provider.strip().lower()
    if provider not in SSO_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"provider must be one of {list(SSO_PROVIDERS)}",
        )
    for url in (body.authorization_endpoint, body.token_endpoint, body.jwks_uri):
        assert_public_http_url(url)

    config: dict[str, Any] = {
        "client_secret_ref": body.client_secret_ref,
        "authorization_endpoint": body.authorization_endpoint,
        "token_endpoint": body.token_endpoint,
        "jwks_uri": body.jwks_uri,
        "redirect_uri": body.redirect_uri,
        "scopes": body.scopes,
        "token_endpoint_auth_method": body.token_endpoint_auth_method,
    }
    # Fingerprint the secret now (if present) so a later out-of-band rotation is
    # detected at login time instead of failing opaquely at the IdP.
    live_secret = resolver.resolve(body.client_secret_ref)
    if live_secret:
        config["client_secret_sha256"] = fingerprint_secret(live_secret)

    connection = SsoConnection(
        org_id=org_id,
        provider=provider,
        issuer=body.issuer,
        client_id=body.client_id,
        config=config,
        active=True,
    )
    db.add(connection)
    await db.flush()
    await record_audit(
        db, action="sso.connection.create", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="sso_connection", resource_id=str(connection.id),
    )
    await db.flush()
    return _to_out(connection)


@router.get("/orgs/{org_id}/connections", response_model=list[SsoConnectionOut])
async def list_connections(
    org_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SsoConnectionOut]:
    _require_sso_enabled()
    await require_org_role(db, org_id, current, ORG_ROLE_ADMIN)
    result = await db.execute(
        select(SsoConnection)
        .where(SsoConnection.org_id == org_id)
        .order_by(SsoConnection.created_at)
    )
    return [_to_out(c) for c in result.scalars().all()]


async def _get_connection_for_admin(
    db: AsyncSession, org_id: uuid.UUID, connection_id: uuid.UUID, current: CurrentUser
) -> SsoConnection:
    await require_org_role(db, org_id, current, ORG_ROLE_ADMIN)
    connection = (
        await db.execute(
            select(SsoConnection).where(
                SsoConnection.id == connection_id, SsoConnection.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise _CONNECTION_NOT_FOUND
    return connection


@router.patch(
    "/orgs/{org_id}/connections/{connection_id}", response_model=SsoConnectionOut
)
async def update_connection(
    org_id: uuid.UUID,
    connection_id: uuid.UUID,
    body: SsoConnectionUpdate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SsoConnectionOut:
    _require_sso_enabled()
    connection = await _get_connection_for_admin(db, org_id, connection_id, current)
    config: dict[str, Any] = dict(connection.config or {})
    for field in (
        "client_secret_ref",
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
        "redirect_uri",
        "scopes",
        "token_endpoint_auth_method",
    ):
        value = getattr(body, field)
        if value is not None:
            if field.endswith(("endpoint", "uri")):
                assert_public_http_url(str(value))
            config[field] = value
    if body.client_secret_ref is not None:
        # New ref => the old fingerprint is meaningless.
        config.pop("client_secret_sha256", None)
    if body.client_id is not None:
        connection.client_id = body.client_id
    if body.active is not None:
        connection.active = body.active
    connection.config = config
    connection.updated_at = utcnow()
    await record_audit(
        db, action="sso.connection.update", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="sso_connection", resource_id=str(connection.id),
    )
    await db.flush()
    return _to_out(connection)


@router.delete(
    "/orgs/{org_id}/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_connection(
    org_id: uuid.UUID,
    connection_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_sso_enabled()
    connection = await _get_connection_for_admin(db, org_id, connection_id, current)
    await db.delete(connection)
    await record_audit(
        db, action="sso.connection.delete", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="sso_connection", resource_id=str(connection_id),
    )
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# OIDC authorization-code flow
# ---------------------------------------------------------------------------
@router.post("/oidc/{connection_id}/authorize", response_model=SsoAuthorizeOut)
async def start_oidc_login(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SsoAuthorizeOut:
    """Begin a login: returns the IdP URL plus the signed state to echo back."""
    _require_sso_enabled()
    connection = (
        await db.execute(
            select(SsoConnection).where(
                SsoConnection.id == connection_id,
                SsoConnection.provider == "oidc",
                SsoConnection.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise _CONNECTION_NOT_FOUND
    state, nonce = issue_state(connection.id)
    url = build_authorization_url(
        config=dict(connection.config or {}),
        client_id=connection.client_id,
        state=state,
        nonce=nonce,
    )
    return SsoAuthorizeOut(authorization_url=url, state=state)


@router.post("/oidc/callback", response_model=SsoLoginOut)
async def oidc_callback(
    body: SsoCallbackIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    client: OidcClient = Depends(get_oidc_client),
    resolver: SecretResolver = Depends(get_secret_resolver),
) -> SsoLoginOut:
    """Complete the authorization-code flow and mint a ComputeCoin session.

    Order matters: state signature first (cheapest, rejects forgeries without
    touching the IdP), then code exchange, then full id_token verification —
    signature, issuer, audience, expiry and the nonce bound into our state.
    """
    _require_sso_enabled()
    claims_state = verify_state(body.state)
    connection = (
        await db.execute(
            select(SsoConnection).where(
                SsoConnection.id == uuid.UUID(str(claims_state["cid"])),
                SsoConnection.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise _CONNECTION_NOT_FOUND

    config: dict[str, Any] = dict(connection.config or {})
    client_secret = resolve_client_secret(config, resolver)
    token_endpoint = str(config.get("token_endpoint") or "")
    if not token_endpoint:
        raise SsoError("sso_connection_incomplete", status.HTTP_503_SERVICE_UNAVAILABLE)

    # client_secret_basic is the OAuth 2.0 DEFAULT and some IdPs accept nothing
    # else; client_secret_post is what this code originally assumed. The method
    # comes from the connection so an administrator can match their IdP.
    auth_method = str(
        config.get("token_endpoint_auth_method") or "client_secret_post"
    ).strip().lower()
    if auth_method not in ("client_secret_post", "client_secret_basic"):
        raise SsoError("sso_unsupported_token_auth_method")

    token_request = {
        "grant_type": "authorization_code",
        "code": body.code,
        "redirect_uri": str(config.get("redirect_uri") or ""),
        "client_id": connection.client_id,
    }
    basic_auth: tuple[str, str] | None = None
    if auth_method == "client_secret_basic":
        # RFC 6749 2.3.1: with Basic, the secret goes in the header and MUST
        # NOT be duplicated in the body.
        basic_auth = (connection.client_id, client_secret)
    else:
        token_request["client_secret"] = client_secret

    token_response = await client.exchange_code(
        token_endpoint,
        token_request,
        basic_auth=basic_auth,
    )
    id_token = token_response.get("id_token")
    if not id_token:
        raise SsoError("sso_id_token_missing")

    jwks = await client.fetch_jwks(str(config.get("jwks_uri") or ""))
    claims = verify_id_token(
        id_token=str(id_token),
        jwks=jwks,
        client_secret=client_secret,
        issuer=connection.issuer,
        client_id=connection.client_id,
        nonce=str(claims_state["nonce"]),
    )
    email = extract_email(claims)

    user = await get_user_by_email(db, email)
    provisioned = False
    if user is not None:
        # An IdP assertion must NEVER be allowed to adopt a pre-existing local
        # account. Every endpoint of this connection's IdP (issuer, client_id,
        # jwks_uri, token_endpoint) is supplied by whoever created the
        # connection, and verify_id_token checks the token against exactly those
        # attacker-registered values — so "the signature is valid" proves only
        # that the registrant signed it. Without this gate, registering an org,
        # pointing a connection at an IdP you control and asserting
        # ``email: admin@…`` mints a full platform session for that admin.
        #
        # Federated identity is therefore bound to the org: an existing account
        # may sign in through a connection only if it is ALREADY a member of
        # that org (a link the org established out of band), and never if it
        # holds a privileged platform role.
        existing_roles = await get_user_roles(db, user.id)
        if _PRIVILEGED_ROLES.intersection(existing_roles):
            await record_audit(
                db, action="sso.login", outcome="failure", actor_user_id=user.id,
                ip=client_ip(request), resource_type="sso_connection",
                resource_id=str(connection.id), reason="privileged_account_refused",
            )
            await db.flush()
            raise SsoError("sso_privileged_account_refused", status.HTTP_403_FORBIDDEN)
        if await get_membership(db, connection.org_id, user.id) is None:
            await record_audit(
                db, action="sso.login", outcome="failure", actor_user_id=user.id,
                ip=client_ip(request), resource_type="sso_connection",
                resource_id=str(connection.id), reason="identity_not_linked_to_org",
            )
            await db.flush()
            raise SsoError("sso_identity_not_linked", status.HTTP_403_FORBIDDEN)
    if user is None:
        # Just-in-time provisioning. The password hash is a random value the
        # user never learns: SSO identities sign in through the IdP only, and a
        # nullable hash would weaken the login path's invariants.
        user = User(
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=user.id, role=Role.customer))
        await db.flush()
        provisioned = True
    elif not user.email_verified:
        # The IdP vouches for this mailbox, so treat it as verified.
        user.email_verified = True

    if await get_membership(db, connection.org_id, user.id) is None:
        db.add(
            OrganizationMember(
                org_id=connection.org_id, user_id=user.id, role=ORG_ROLE_MEMBER
            )
        )
        await db.flush()

    roles = await get_user_roles(db, user.id)
    access, refresh, expires_in, _ = await issue_session(
        db,
        user=user,
        roles=roles,
        user_agent=request.headers.get("user-agent"),
        ip=client_ip(request),
    )
    await record_audit(
        db, action="sso.login", outcome="success", actor_user_id=user.id,
        ip=client_ip(request), resource_type="sso_connection", resource_id=str(connection.id),
        reason="provisioned" if provisioned else "existing",
    )
    await db.flush()
    return SsoLoginOut(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        org_id=connection.org_id,
        user_id=user.id,
        email=user.email,
        provisioned=provisioned,
    )


__all__ = ["router"]
