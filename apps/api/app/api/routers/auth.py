"""Authentication & account-recovery endpoints."""

from __future__ import annotations

import datetime as dt
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import rate_limiter
from app.core.security import (
    generate_opaque_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.db.base import ensure_aware, utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user
from app.models.enums import Role, UserStatus
from app.models.user import (
    EmailVerificationToken,
    PasswordResetToken,
    User,
    UserRole,
)
from app.schemas.auth import (
    ChangePasswordRequest,
    CurrentUser,
    DeleteAccountRequest,
    ProfileUpdate,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    VerifyEmailRequest,
)
from app.services.audit import record_audit
from app.services.auth import (
    find_session_by_refresh,
    get_user_by_email,
    get_user_roles,
    issue_session,
    revoke_all_user_sessions,
    revoke_chain,
    revoke_session,
)
from app.services.email import send_password_reset_email, send_verification_email

router = APIRouter(prefix="/auth", tags=["auth"])

_GENERIC_LOGIN_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
)


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    csrf_token: str,
    expires_in: int,
) -> None:
    """Set hardened httpOnly authentication cookies and double-submit CSRF cookie."""
    samesite = "none" if settings.cookie_secure else settings.COOKIE_SAMESITE
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=samesite,
        max_age=expires_in,
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=samesite,
        max_age=settings.REFRESH_TOKEN_TTL_SECONDS,
        path="/",
    )
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,  # Readable by client JS to double-submit in X-CSRF-Token header
        secure=settings.cookie_secure,
        samesite=samesite,
        max_age=settings.REFRESH_TOKEN_TTL_SECONDS,
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    """Clear all authentication and CSRF cookies on logout."""
    samesite = "none" if settings.cookie_secure else settings.COOKIE_SAMESITE
    response.delete_cookie("access_token", path="/", samesite=samesite, secure=settings.cookie_secure)
    response.delete_cookie("refresh_token", path="/", samesite=samesite, secure=settings.cookie_secure)
    response.delete_cookie("csrf_token", path="/", samesite=samesite, secure=settings.cookie_secure)


def _enforce_rate_limit(key: str, limit: int, window: int) -> None:
    if not rate_limiter.allow(key, limit, window):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests, please try again later",
        )


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    ip = client_ip(request)
    _enforce_rate_limit(
        f"register:{ip}", settings.register_rate_limit, settings.REGISTER_RATE_WINDOW_SECONDS
    )

    email = body.email.strip().lower()
    existing = await get_user_by_email(db, email)
    if existing is not None:
        # Avoid user enumeration: generic conflict message.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Unable to register with these details"
        )

    user = User(email=email, password_hash=hash_password(body.password), email_verified=False)
    db.add(user)
    await db.flush()

    db.add(UserRole(user_id=user.id, role=Role.customer))

    raw_token = generate_opaque_token()
    verification = EmailVerificationToken(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=utcnow() + dt.timedelta(seconds=settings.EMAIL_VERIFICATION_TTL_SECONDS),
    )
    db.add(verification)
    await record_audit(
        db, action="user.register", outcome="success", actor_user_id=user.id, ip=ip,
        resource_type="user", resource_id=str(user.id),
    )
    await db.flush()
    await send_verification_email(user.email, raw_token)

    return RegisterResponse(
        user_id=user.id,
        email=user.email,
        email_verification_token=raw_token if settings.expose_dev_tokens else None,
    )


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    body: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    token_hash = hash_token(body.token)
    result = await db.execute(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == token_hash
        )
    )
    token = result.scalar_one_or_none()
    now = utcnow()
    if token is None or token.used_at is not None or ensure_aware(token.expires_at) <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token"
        )

    token.used_at = now
    user_result = await db.execute(select(User).where(User.id == token.user_id))
    user = user_result.scalar_one_or_none()
    if user is not None:
        user.email_verified = True
    await db.flush()
    return MessageResponse(message="Email verified")


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Email a fresh verification link to the signed-in, still-unverified user.

    The link from registration expires (24h) and can be lost; without this an
    unverified account is locked out of billing and compute for good. Requires a
    session, so it cannot be used to probe which addresses are registered, and is
    rate-limited per account so it cannot be used to spam an inbox."""
    _enforce_rate_limit(f"resend_verify:{current.id}", 3, 3600)
    if current.email_verified:
        return MessageResponse(message="Email already verified")

    raw_token = generate_opaque_token()
    db.add(
        EmailVerificationToken(
            user_id=current.id,
            token_hash=hash_token(raw_token),
            expires_at=utcnow() + dt.timedelta(seconds=settings.EMAIL_VERIFICATION_TTL_SECONDS),
        )
    )
    await record_audit(
        db, action="user.resend_verification", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="user", resource_id=str(current.id),
    )
    await db.flush()
    await send_verification_email(current.email, raw_token)
    return MessageResponse(message="Verification email sent")


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    ip = client_ip(request)
    email = body.email.strip().lower()
    # Rate limit by IP and by target email to curb credential stuffing.
    _enforce_rate_limit(
        f"login:{ip}", settings.login_ip_rate_limit, settings.LOGIN_IP_RATE_WINDOW_SECONDS
    )
    _enforce_rate_limit(
        f"login:{email}", settings.LOGIN_RATE_LIMIT, settings.LOGIN_RATE_WINDOW_SECONDS
    )

    user = await get_user_by_email(db, email)
    user_agent = request.headers.get("user-agent")

    if user is None or not verify_password(user.password_hash, body.password):
        await record_audit(
            db, action="auth.login", outcome="failure", ip=ip,
            actor_user_id=user.id if user else None, reason="invalid_credentials",
        )
        await db.commit()
        raise _GENERIC_LOGIN_ERROR

    if user.status.value != "active":
        await record_audit(
            db, action="auth.login", outcome="failure", ip=ip,
            actor_user_id=user.id, reason="account_not_active",
        )
        await db.commit()
        raise _GENERIC_LOGIN_ERROR

    roles = await get_user_roles(db, user.id)
    access, refresh, expires_in, _ = await issue_session(
        db, user=user, roles=roles, user_agent=user_agent, ip=ip
    )
    await record_audit(
        db, action="auth.login", outcome="success", actor_user_id=user.id, ip=ip
    )

    csrf_token = secrets.token_hex(32)
    set_auth_cookies(
        response=response,
        access_token=access,
        refresh_token=refresh,
        csrf_token=csrf_token,
        expires_in=expires_in,
    )

    await db.flush()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        csrf_token=csrf_token,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    ip = client_ip(request)
    raw_refresh = body.refresh_token if (body and body.refresh_token) else request.cookies.get("refresh_token")
    if not raw_refresh:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    # If using ambient cookie refresh, enforce double-submit CSRF protection
    if not (body and body.refresh_token) and "refresh_token" in request.cookies:
        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("x-csrf-token")
        if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed: missing or invalid X-CSRF-Token header",
            )

    session = await find_session_by_refresh(db, raw_refresh)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    now = utcnow()

    # Reuse detection: a token that was already rotated (revoked) is being
    # replayed -> revoke the entire chain and refuse.
    if session.revoked_at is not None:
        revoked = await revoke_chain(db, session)
        await record_audit(
            db, action="auth.refresh", outcome="failure", actor_user_id=session.user_id,
            ip=ip, reason=f"refresh_reuse_detected_chain_revoked={revoked}",
        )
        # Persist chain revocation + audit before signalling failure (get_db
        # would otherwise roll back on the raised exception).
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    if ensure_aware(session.expires_at) <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    user_result = await db.execute(select(User).where(User.id == session.user_id))
    user = user_result.scalar_one_or_none()
    if user is None or user.status.value != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    # Rotate: revoke the presented session, issue a new one chained to it.
    await revoke_session(db, session)
    roles = await get_user_roles(db, user.id)
    access, new_refresh, expires_in, _ = await issue_session(
        db,
        user=user,
        roles=roles,
        parent_id=session.id,
        user_agent=request.headers.get("user-agent"),
        ip=ip,
    )
    await record_audit(
        db, action="auth.refresh", outcome="success", actor_user_id=user.id, ip=ip
    )

    new_csrf_token = secrets.token_hex(32)
    set_auth_cookies(
        response=response,
        access_token=access,
        refresh_token=new_refresh,
        csrf_token=new_csrf_token,
        expires_in=expires_in,
    )

    await db.flush()
    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=expires_in,
        csrf_token=new_csrf_token,
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    body: LogoutRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    raw_refresh = body.refresh_token if (body and body.refresh_token) else request.cookies.get("refresh_token")
    if not (body and body.refresh_token) and "refresh_token" in request.cookies:
        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("x-csrf-token")
        if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed: missing or invalid X-CSRF-Token header",
            )

    if raw_refresh:
        session = await find_session_by_refresh(db, raw_refresh)
        if session is not None:
            await revoke_session(db, session)

    clear_auth_cookies(response)
    await db.flush()
    # Always generic so logout doesn't leak token validity.
    return MessageResponse(message="Logged out")


@router.get("/me", response_model=CurrentUser)
async def get_me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Return authenticated user profile for web and API clients."""
    return current_user


@router.patch("/me", response_model=CurrentUser)
async def update_me(
    body: ProfileUpdate,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Update the signed-in user's profile (display name)."""
    user = await db.get(User, current.id)
    name = (body.display_name or "").strip()
    user.display_name = name or None
    await db.flush()
    return current.model_copy(update={"display_name": user.display_name})


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Change the password. Needs the current one, and signs out every other device."""
    _enforce_rate_limit(f"change_password:{current.id}", 5, 900)
    user = await db.get(User, current.id)
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="current_password_incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="new_password_same_as_current")
    user.password_hash = hash_password(body.new_password)
    # A changed password must end every existing session, as a password reset does.
    await revoke_all_user_sessions(db, user.id)
    await record_audit(
        db, action="user.password_change", outcome="success", actor_user_id=user.id,
        ip=client_ip(request), resource_type="user", resource_id=str(user.id),
    )
    await db.flush()
    return MessageResponse(message="Password changed. Please sign in again.")


@router.post("/delete-account", response_model=MessageResponse)
async def delete_account(
    body: DeleteAccountRequest,
    request: Request,
    response: Response,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Delete (anonymise) the signed-in account.

    The user row is anonymised rather than removed: jobs, invoices and the
    double-entry ledger reference it and must stay intact and balanced. Everything
    that could still act for the account is revoked (sessions, API keys, enrollment
    tokens, machines, webhooks). Refused while money or work is still in flight.
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from app.models.billing import Dispute, ProviderPayout
    from app.models.jobs import Job
    from app.models.org import ApiKey, Project
    from app.models.provider import EnrollmentToken, Provider, ProviderMachine
    from app.models.wallet import LinkedWallet
    from app.models.webhooks import WebhookEndpoint
    from app.services import billing as billing_svc
    from app.services import job_fsm as fsm
    from app.services import ledger

    _enforce_rate_limit(f"delete_account:{current.id}", 5, 900)
    if "administrator" in current.roles:
        # The last line of defence for the platform: remove admin rights deliberately first.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="administrator_cannot_self_delete")

    user = await db.get(User, current.id)
    if not verify_password(user.password_hash, body.password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password_incorrect")

    active = await db.scalar(
        select(func.count()).select_from(Job).where(
            Job.customer_id == user.id, Job.status.notin_(list(fsm.TERMINAL_STATES))
        )
    )
    if active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="account_has_active_jobs")
    open_disputes = await db.scalar(
        select(func.count()).select_from(Dispute).where(
            Dispute.customer_id == user.id, Dispute.status == "open"
        )
    )
    if open_disputes:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="account_has_open_disputes")

    provider = (await db.execute(select(Provider).where(Provider.user_id == user.id))).scalar_one_or_none()
    if provider is not None:
        pending = await db.scalar(
            select(func.count()).select_from(ProviderPayout).where(
                ProviderPayout.provider_id == provider.id, ProviderPayout.status == "requested"
            )
        )
        if pending:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="account_has_pending_payouts")

    account = await ledger.get_or_create_customer_account(db, user.id)
    reserved = await billing_svc.customer_reserved_minor(db, user.id)
    if reserved:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="account_has_held_credits")
    if account.balance_minor > 0 and not body.acknowledge_forfeit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="credits_would_be_forfeited",
            headers={"X-Forfeit-Amount": ledger.minor_to_str(account.balance_minor)},
        )

    now = utcnow()
    original_email_hash = hash_token(user.email)  # audit correlation without keeping the address
    await revoke_all_user_sessions(db, user.id)
    await db.execute(
        sa_update(ApiKey)
        .where(ApiKey.project_id.in_(select(Project.id).where(Project.owner_id == user.id)), ApiKey.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.execute(
        sa_update(WebhookEndpoint).where(WebhookEndpoint.customer_id == user.id).values(active=False)
    )
    # Unlink wallets so a deposit from one can never be credited to a deleted account.
    await db.execute(sa_delete(LinkedWallet).where(LinkedWallet.user_id == user.id))
    if provider is not None:
        provider.display_name = None
        provider.contact_email = None
        provider.status = "deleted"
        await db.execute(
            sa_update(EnrollmentToken)
            .where(EnrollmentToken.provider_id == provider.id, EnrollmentToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await db.execute(
            sa_update(ProviderMachine)
            .where(ProviderMachine.provider_id == provider.id)
            .values(status="deregistered", availability="offline", signing_key="")
        )
    await db.execute(sa_update(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id).values(used_at=now))
    await db.execute(sa_update(PasswordResetToken).where(PasswordResetToken.user_id == user.id).values(used_at=now))

    user.email = f"deleted-{user.id}@deleted.invalid"
    user.display_name = None
    user.password_hash = hash_password(generate_opaque_token())  # nobody knows it
    user.email_verified = False
    user.status = UserStatus.deleted
    user.deleted_at = now
    await record_audit(
        db, action="user.delete", outcome="success", actor_user_id=user.id,
        ip=client_ip(request), resource_type="user", resource_id=str(user.id),
        reason=f"forfeited={ledger.minor_to_str(account.balance_minor)} email_sha={original_email_hash[:12]}",
    )
    await db.flush()
    clear_auth_cookies(response)
    return MessageResponse(message="Account deleted")


@router.post("/password-reset/request", response_model=PasswordResetRequestResponse)
async def password_reset_request(
    body: PasswordResetRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PasswordResetRequestResponse:
    ip = client_ip(request)
    email = body.email.strip().lower()
    _enforce_rate_limit(
        f"pwreset:{ip}",
        settings.PASSWORD_RESET_RATE_LIMIT,
        settings.PASSWORD_RESET_RATE_WINDOW_SECONDS,
    )

    user = await get_user_by_email(db, email)
    raw_token: str | None = None
    if user is not None:
        raw_token = generate_opaque_token()
        reset = PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=utcnow() + dt.timedelta(seconds=settings.PASSWORD_RESET_TTL_SECONDS),
        )
        db.add(reset)
        await record_audit(
            db, action="auth.password_reset_request", outcome="success",
            actor_user_id=user.id, ip=ip,
        )
        await send_password_reset_email(user.email, raw_token)
    await db.flush()

    # Generic message either way to avoid user enumeration.
    return PasswordResetRequestResponse(
        message="If the account exists, a reset token has been issued",
        password_reset_token=raw_token if settings.expose_dev_tokens else None,
    )


@router.post("/password-reset/confirm", response_model=MessageResponse)
async def password_reset_confirm(
    body: PasswordResetConfirm,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    ip = client_ip(request)
    token_hash = hash_token(body.token)
    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    token = result.scalar_one_or_none()
    now = utcnow()
    if token is None or token.used_at is not None or ensure_aware(token.expires_at) <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token"
        )

    token.used_at = now
    user_result = await db.execute(select(User).where(User.id == token.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token"
        )

    user.password_hash = hash_password(body.new_password)
    # Revoke all sessions on password change.
    await revoke_all_user_sessions(db, user.id)
    await record_audit(
        db, action="auth.password_reset_confirm", outcome="success",
        actor_user_id=user.id, ip=ip,
    )
    await db.flush()
    return MessageResponse(message="Password updated")
