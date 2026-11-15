"""Interactive sessions service (U10): create, claim, activate, heartbeat, stop, auto-stop, billing.

Money reuses the existing ledger and billing helpers without changing them: a worst-case HOLD at create
(``sess-hold:<id>``), and at the end one CAPTURE for the GPU-seconds the session was ACTIVE plus a RELEASE of
the rest, mirroring ``billing.settle_job``. Billing runs from ``started_at`` (the host reported the container
running) to ``stopped_at``. Pending and starting time is never billed.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import re
import secrets
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.sessions import (
    SESSION_ACTIVE,
    SESSION_FAILED,
    SESSION_LIVE,
    SESSION_PENDING,
    SESSION_STARTING,
    SESSION_STOPPED,
    STOP_HOST_ENDED,
    STOP_HOST_LOST,
    STOP_IDLE,
    STOP_MAX_DURATION,
    STOP_START_FAILED,
    STOP_UNCLAIMED,
    ComputeSession,
)
from app.services import billing, ledger
from app.services import rental as rental_svc
from app.services.network_policy import validate_network_policy
from app.services.pricing import get_active_fee_rate
from app.services.spend_limits import check_spend_allowed

PRIVATE_TIERS = ("sealed", "sealed_pro", "confidential")
ALLOWED_TIERS = ("container", "gvisor", "microvm")
IMAGE_RE = re.compile(r"^[a-zA-Z0-9][\w./:-]*@sha256:[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{64}$")
TOKEN_TTL_SECONDS = 60
HOST_HEARTBEAT_GRACE_SECONDS = 90
UNCLAIMED_SECONDS = 600
START_TIMEOUT_SECONDS = 300
MIN_IDLE_SECONDS = 60
MAX_IDLE_SECONDS = 24 * 3600
MAX_DURATION_CAP_SECONDS = 24 * 3600


class SessionError(Exception):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.status = status


def _cfg(name: str, default):
    return getattr(settings, name, default)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def validate_request(
    *,
    isolation_tier: str,
    kit_image: str | None,
    image_digest: str | None,
    gpu_count: int,
    max_duration_seconds: int,
    idle_timeout_seconds: int,
    client_pubkey: str,
    network_policy: object,
    mode: str,
    tcp_port: int | None,
    rental: bool = False,
) -> dict | None:
    """Pure checks; returns the validated network policy. Raises SessionError. ``rental`` marks a container-rental session
    (a template or a customer image on an opted-in host): it replaces the old "custom images are off" gate, which stays
    for every non-rental request."""
    tier = (isolation_tier or "container").lower()
    if tier in PRIVATE_TIERS:
        raise SessionError("sessions_not_available_for_private_tiers")  # U18 adds them
    if tier not in ALLOWED_TIERS:
        raise SessionError("unsupported_isolation_tier")
    if bool(kit_image) == bool(image_digest):
        raise SessionError("give_exactly_one_of_kit_ref_or_image_digest")
    if image_digest:
        if not rental and not _cfg("SESSIONS_ALLOW_BYOC_IMAGE", False):
            raise SessionError("custom_images_not_enabled_for_sessions")
        if not IMAGE_RE.fullmatch(image_digest):
            raise SessionError("image_must_be_pinned_by_sha256_digest")
    if not 0 <= gpu_count <= 8:
        raise SessionError("gpu_count_out_of_range")
    if not 60 <= max_duration_seconds <= MAX_DURATION_CAP_SECONDS:
        raise SessionError("max_duration_out_of_range")
    if not MIN_IDLE_SECONDS <= idle_timeout_seconds <= min(MAX_IDLE_SECONDS, max_duration_seconds):
        raise SessionError("idle_timeout_out_of_range")
    if not HEX32.match(client_pubkey or ""):
        raise SessionError("client_pubkey_must_be_32_byte_hex")
    if mode not in ("pty", "tcp"):
        raise SessionError("mode_must_be_pty_or_tcp")
    if mode == "tcp" and not (tcp_port and 1 <= tcp_port <= 65535):
        raise SessionError("tcp_port_required")
    try:
        return validate_network_policy(network_policy, isolation_tier=tier)
    except Exception as exc:  # NetworkPolicyError
        raise SessionError(f"network_policy_invalid:{exc}"[:120]) from exc


async def _price_parts(db: AsyncSession, price: decimal.Decimal, gpus: int, seconds: int) -> tuple[int, int]:
    fee = await get_active_fee_rate(db)
    return billing._meter_actual_cost(
        price_per_hour=price, gpu_count=max(gpus, 1), fee_rate=fee, actual_seconds=decimal.Decimal(seconds)
    )


def _prepare_rental(
    *, template, kit_image, image_digest, isolation_tier, accept_rental_terms, network_policy, mode, tcp_port, command,
):
    """Rules 3 to 7 of section 7.1. Returns (network policy, image digest, mode, tcp port, command, template slug)."""
    if template and (kit_image or image_digest):
        raise SessionError("template_excludes_kit_ref_and_image_digest")
    if template and not rental_svc.rental_enabled():
        raise SessionError("container_rental_not_enabled")
    if not accept_rental_terms:
        raise SessionError("rental_terms_not_accepted")
    if (isolation_tier or "container").lower() != "container":
        raise SessionError("rental_requires_container_tier")
    declared: list[str] | None = None
    slug = None
    if template:
        tpl = rental_svc.get_template(template)
        if tpl is None:
            raise SessionError("template_not_found", 404)
        slug, image_digest, mode, tcp_port = tpl["slug"], tpl["digest"], tpl["mode"], tpl["tcp_port"]
        command = list(tpl["command"]) if tpl["command"] else None  # None: the image's ENTRYPOINT runs, no override
        declared = list(tpl["default_egress"])
    else:
        if not rental_svc.IMAGE_RE.fullmatch(image_digest or ""):
            raise SessionError("image_must_be_pinned_by_sha256_digest")
        if not rental_svc.registry_allowed(image_digest):
            raise SessionError("image_registry_not_allowed")
        try:
            command = rental_svc.clean_command(command)
        except rental_svc.RentalError as exc:
            raise SessionError(exc.code, exc.status) from exc
    try:
        asked_policy = validate_network_policy(network_policy, isolation_tier="container")
    except Exception as exc:  # NetworkPolicyError
        raise SessionError(f"network_policy_invalid:{exc}"[:120]) from exc
    asked = list((asked_policy or {}).get("egress") or [])
    if declared is not None:
        if asked and not set(asked) <= set(declared):
            raise SessionError("egress_not_declared_by_template")
        final = sorted(set(asked or declared))
    else:
        final = sorted(set(asked))
    allow = rental_svc.egress_allowlist()
    if any(e not in allow for e in final):
        raise SessionError("egress_not_allowed")
    return ({"egress": final} if final else None), image_digest, mode, tcp_port, command, slug


def _rental_limits(workspace_gb: int, memory_mb: int | None, cpus: int | None) -> tuple[int, int]:
    """Rule 9. Returns the (memory_mb, cpus) to store. Booleans are not numbers here."""
    memory_mb = rental_svc.DEFAULT_MEMORY_MB if memory_mb is None else memory_mb
    cpus = rental_svc.DEFAULT_CPUS if cpus is None else cpus
    for v in (workspace_gb, memory_mb, cpus):
        if not isinstance(v, int) or isinstance(v, bool):
            raise SessionError("rental_limit_out_of_range")
    if not 0 <= workspace_gb <= int(_cfg("RENTAL_WORKSPACE_MAX_GB", 100)):
        raise SessionError("rental_limit_out_of_range")
    if not rental_svc.MIN_MEMORY_MB <= memory_mb <= int(_cfg("RENTAL_MAX_MEMORY_MB", 262144)):
        raise SessionError("rental_limit_out_of_range")
    if not 1 <= cpus <= int(_cfg("RENTAL_MAX_CPUS", 64)):
        raise SessionError("rental_limit_out_of_range")
    return memory_mb, cpus


async def machine_has_running_job(db: AsyncSession, machine_id: uuid.UUID) -> bool:
    """A job is RUNNING on this machine (it holds the GPU). Used so a session is not claimed on top of it."""
    from app.models.jobs import Job
    from app.services import job_fsm as fsm

    return (
        await db.execute(select(Job.id).where(Job.machine_id == machine_id, Job.status == fsm.RUNNING).limit(1))
    ).first() is not None


async def machine_has_gpu_session(db: AsyncSession, machine_id: uuid.UUID) -> bool:
    """A session that uses a GPU is starting or active on this machine. Used so a job is not claimed on top of it."""
    return (
        await db.execute(
            select(ComputeSession.id).where(
                ComputeSession.machine_id == machine_id,
                ComputeSession.status.in_((SESSION_STARTING, SESSION_ACTIVE)),
                ComputeSession.gpu_count > 0,
            ).limit(1)
        )
    ).first() is not None


async def create_session(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    project_id: uuid.UUID | None,
    kit_ref: str | None,
    kit_image: str | None,
    kit_session: dict | None,
    image_digest: str | None,
    gpu_count: int,
    max_price_per_gpu_hour: decimal.Decimal,
    max_duration_seconds: int,
    idle_timeout_seconds: int,
    client_pubkey: str,
    isolation_tier: str = "container",
    network_policy: object = None,
    mode: str = "pty",
    tcp_port: int | None = None,
    kit_egress: list[str] | None = None,
    template: str | None = None,
    machine_id: uuid.UUID | None = None,
    workspace_gb: int = 0,
    memory_mb: int | None = None,
    cpus: int | None = None,
    command: list | None = None,
    inputs: list | None = None,
    accept_rental_terms: bool = False,
) -> ComputeSession:
    # A kit's own session block (command, mode, port, idle cap) is authoritative over the request. It was validated
    # at publish; it is re-validated here (fail closed) because the manifest is data.
    ks = kit_session or {}
    requested_command = command  # what the request asked for; only a rental may use it
    command = None
    if kit_image and kit_session is not None:
        from app.services.kits import validate_session_block

        if validate_session_block(ks):
            raise SessionError("kit_session_block_invalid")
        command = list(ks["command"]) if ks.get("command") else None
        mode = "pty" if ks["mode"] == "terminal" else "tcp"
        tcp_port = ks.get("tcp_port") if mode == "tcp" else None
        if ks.get("idle_timeout_seconds"):
            idle_timeout_seconds = min(idle_timeout_seconds, int(ks["idle_timeout_seconds"]))
        # No network unless the kit declared egress; a request may only narrow the kit's list.
        declared = sorted(set(kit_egress or []))
        asked = (network_policy or {}).get("egress") if isinstance(network_policy, dict) else None
        if asked and not set(asked) <= set(declared):
            raise SessionError("egress_not_declared_by_kit")
        network_policy = {"egress": sorted(set(asked)) if asked else declared} if (asked or declared) else None
    is_rental = bool(template) or (bool(image_digest) and not kit_image and rental_svc.rental_enabled())
    rental_inputs: list[dict] = []
    if is_rental:
        if rental_svc.relay_misconfigured():  # fail now, loudly, rather than start a rental nobody can connect to
            raise SessionError("session_relay_not_ready", 503)
        # docs/architecture/RENTAL-container-rental-design.md section 7.1, rules 3 to 10, in that order.
        network_policy, image_digest, mode, tcp_port, command, template_slug = _prepare_rental(
            template=template, kit_image=kit_image, image_digest=image_digest, isolation_tier=isolation_tier,
            accept_rental_terms=accept_rental_terms, network_policy=network_policy, mode=mode, tcp_port=tcp_port,
            command=requested_command,
        )
        memory_mb, cpus = _rental_limits(workspace_gb, memory_mb, cpus)
        if machine_id is not None:
            try:
                await rental_svc.check_target_machine(db, machine_id, require_flag=True, needs_egress=bool(network_policy))
            except rental_svc.RentalError as exc:
                raise SessionError(exc.code, exc.status) from exc
        try:
            rental_inputs = await rental_svc.check_inputs(db, customer_id, inputs or [])
        except rental_svc.RentalError as exc:
            raise SessionError(exc.code, exc.status) from exc
        if rental_inputs and workspace_gb < 1:
            raise SessionError("workspace_required_for_inputs")
    else:
        template_slug = None
        if template or workspace_gb != 0 or memory_mb is not None or cpus is not None:
            raise SessionError("rental_limit_out_of_range")
        if requested_command is not None or inputs:
            raise SessionError("rental_fields_require_rental")
        if machine_id is not None:
            try:
                await rental_svc.check_target_machine(db, machine_id, require_flag=False)
            except rental_svc.RentalError as exc:
                raise SessionError(exc.code, exc.status) from exc
    policy = validate_request(
        isolation_tier=isolation_tier, kit_image=kit_image, image_digest=image_digest, gpu_count=gpu_count,
        max_duration_seconds=max_duration_seconds, idle_timeout_seconds=idle_timeout_seconds,
        client_pubkey=client_pubkey, network_policy=network_policy, mode=mode, tcp_port=tcp_port, rental=is_rental,
    )
    if max_price_per_gpu_hour <= 0:
        raise SessionError("max_price_must_be_positive")
    # Serialize this customer's session creation BEFORE counting live sessions: counting first and locking
    # afterwards let two concurrent creates both read "2 live" and both pass the SESSIONS_MAX_LIVE_PER_USER cap.
    await ledger.lock_customer(db, customer_id)
    live = (
        await db.execute(
            select(ComputeSession.id).where(
                ComputeSession.customer_id == customer_id, ComputeSession.status.in_(SESSION_LIVE)
            )
        )
    ).all()
    if len(live) >= int(_cfg("SESSIONS_MAX_LIVE_PER_USER", 3)):
        raise SessionError("too_many_live_sessions", 429)

    gpu_minor, fee_minor = await _price_parts(db, max_price_per_gpu_hour, gpu_count, max_duration_seconds)
    held = gpu_minor + fee_minor
    sid = uuid.uuid4()
    await check_spend_allowed(db, customer_id, project_id, held)
    if held > 0:
        customer = await ledger.get_or_create_customer_account(db, customer_id)
        holding = await ledger.get_platform_account(db, "holding")
        await ledger.post_transaction(
            db, kind="hold", legs=[(customer, "debit", held), (holding, "credit", held)],
            idempotency_key=f"sess-hold:{sid}", reference_type="session", reference_id=str(sid),
            actor_user_id=customer_id,
        )
    row = ComputeSession(
        id=sid, customer_id=customer_id, project_id=project_id, status=SESSION_PENDING, kit_ref=kit_ref,
        image_digest=kit_image or image_digest, mode=mode, tcp_port=tcp_port, command=command,
        gpu_count=gpu_count, isolation_tier=isolation_tier.lower(), network_policy=policy,
        max_duration_seconds=max_duration_seconds, idle_timeout_seconds=idle_timeout_seconds,
        max_price_per_gpu_hour=max_price_per_gpu_hour, held_minor=held, client_pubkey=client_pubkey,
        rental=is_rental, template=template_slug, requested_machine_id=machine_id,
        workspace_gb=workspace_gb if is_rental else 0, memory_mb=memory_mb if is_rental else None,
        cpus=cpus if is_rental else None, inputs=rental_inputs or None,
        terms_accepted_at=utcnow() if is_rental else None,
    )
    db.add(row)
    await db.flush()
    return row


async def claim_next(db: AsyncSession, machine, *, host_pubkey: str, machine_gpus: int) -> tuple[ComputeSession, str] | None:
    """A host claims the oldest pending session it can run. Returns (session, host_token) or None."""
    if not HEX32.match(host_pubkey or ""):
        raise SessionError("host_pubkey_must_be_32_byte_hex")
    # A session has no scheduler pass, so this claim IS its eligibility gate: a banned machine (proven leak / tamper)
    # or one the owner took out of service (maintenance / throttled / offline) must never be handed a customer's shell.
    if getattr(machine, "is_banned", False) or getattr(machine, "availability", "online") != "online":
        return None
    from app.services.provider import blocked_provider_ids

    if await blocked_provider_ids(db, {machine.provider_id}):
        return None
    from app.services.capability_flags import host_capabilities

    stmt = select(ComputeSession).where(ComputeSession.status == SESSION_PENDING)
    # A session the customer aimed at one machine is only that machine's; a rental only goes to a host that opted in.
    stmt = stmt.where(or_(ComputeSession.requested_machine_id.is_(None), ComputeSession.requested_machine_id == machine.id))
    if "container_rental" not in host_capabilities(machine).flags:
        stmt = stmt.where(ComputeSession.rental.is_(False))
    rows = (await db.execute(stmt.order_by(ComputeSession.created_at).limit(20))).scalars().all()
    can_filter = "egress_proxy" in host_capabilities(machine).flags
    rate_ask = machine.asking_price_per_hour
    job_running: bool | None = None
    for row in rows:
        if row.gpu_count > machine_gpus:
            continue
        if not can_filter and (row.network_policy or {}).get("egress"):
            continue  # a host with no network-filter proxy cannot start it (it refuses rather than widen the network)
        if row.gpu_count > 0:
            if job_running is None:
                job_running = await machine_has_running_job(db, machine.id)
            if job_running:
                continue  # one GPU user at a time: a job is running here
        rate = decimal.Decimal(rate_ask) if rate_ask is not None else row.max_price_per_gpu_hour
        if rate > row.max_price_per_gpu_hour:
            continue
        token = new_token()
        now = utcnow()
        res = await db.execute(  # atomic claim: only one host wins
            update(ComputeSession)
            .where(ComputeSession.id == row.id, ComputeSession.status == SESSION_PENDING)
            .values(
                status=SESSION_STARTING, machine_id=machine.id, provider_id=machine.provider_id,
                rate_per_gpu_hour=rate, host_pubkey=host_pubkey, host_token_hash=hash_token(token),
                host_token_expires_at=now + dt.timedelta(seconds=TOKEN_TTL_SECONDS), host_token_used_at=None,
                last_host_heartbeat_at=now,
            )
        )
        if res.rowcount == 1:
            await db.refresh(row)
            return row, token
    return None


async def refresh_host_token(db: AsyncSession, row: ComputeSession) -> str:
    """The host reconnects after a drop: a new short single-use token (the claim already proved the machine)."""
    token = new_token()
    row.host_token_hash = hash_token(token)
    row.host_token_expires_at = utcnow() + dt.timedelta(seconds=TOKEN_TTL_SECONDS)
    row.host_token_used_at = None
    await db.flush()
    return token


async def issue_browser_token(db: AsyncSession, row: ComputeSession) -> str:
    if row.status not in SESSION_LIVE:
        raise SessionError("session_not_live", 409)
    token = new_token()
    row.browser_token_hash = hash_token(token)
    row.browser_token_expires_at = utcnow() + dt.timedelta(seconds=TOKEN_TTL_SECONDS)
    row.browser_token_used_at = None
    await db.flush()
    return token


async def consume_token(db: AsyncSession, session_id: uuid.UUID, side: str, raw: str) -> ComputeSession | None:
    """Single use: an atomic conditional UPDATE decides, so two sockets can never share one token."""
    col = "host" if side == "host" else "browser"
    c = ComputeSession
    now = utcnow()
    res = await db.execute(
        update(c)
        .where(
            c.id == session_id, c.status.in_(SESSION_LIVE),
            getattr(c, f"{col}_token_hash") == hash_token(raw or ""),
            getattr(c, f"{col}_token_used_at").is_(None),
            getattr(c, f"{col}_token_expires_at") > now,
        )
        .values({f"{col}_token_used_at": now})
    )
    if res.rowcount != 1:
        return None
    await db.commit()
    return (await db.execute(select(c).where(c.id == session_id))).scalar_one_or_none()


async def mark_active(db: AsyncSession, row: ComputeSession) -> None:
    if row.status == SESSION_STARTING:
        now = utcnow()
        row.status = SESSION_ACTIVE
        row.started_at = now
        row.last_activity_at = now
        row.last_host_heartbeat_at = now
        await db.flush()


def deadline_reason(row: ComputeSession, now: dt.datetime) -> str | None:
    """Why this live session must stop now, or None."""
    created = _aware(row.created_at)
    if row.status == SESSION_PENDING and created and (now - created).total_seconds() > UNCLAIMED_SECONDS:
        return STOP_UNCLAIMED
    hb = _aware(row.last_host_heartbeat_at)
    if row.status == SESSION_STARTING:
        if hb and (now - hb).total_seconds() > START_TIMEOUT_SECONDS:
            return STOP_START_FAILED
        return None
    if row.status != SESSION_ACTIVE:
        return None
    started = _aware(row.started_at)
    if started and (now - started).total_seconds() >= row.max_duration_seconds:
        return STOP_MAX_DURATION
    act = _aware(row.last_activity_at) or started
    if act and (now - act).total_seconds() >= row.idle_timeout_seconds:
        return STOP_IDLE
    if hb and (now - hb).total_seconds() > HOST_HEARTBEAT_GRACE_SECONDS:
        return STOP_HOST_LOST
    return None


async def heartbeat(db: AsyncSession, row: ComputeSession, relay_activity: float | None) -> str | None:
    """Host keepalive. Records relay activity, enforces deadlines. Returns a stop reason when the host must stop."""
    now = utcnow()
    if row.status not in SESSION_LIVE:
        return row.stop_reason or "stopped"
    row.last_host_heartbeat_at = now
    if relay_activity is not None:
        seen = dt.datetime.fromtimestamp(relay_activity, tz=dt.timezone.utc)
        cur = _aware(row.last_activity_at)
        if cur is None or seen > cur:
            row.last_activity_at = seen
    reason = deadline_reason(row, now)
    if reason:
        await stop_session(db, row, reason)
        return reason
    await db.flush()
    return None


async def stop_session(db: AsyncSession, row: ComputeSession, reason: str, *, failed: bool = False) -> ComputeSession:
    """End a session and settle the money. Idempotent: ledger keys are per session, a second call is a no-op."""
    if row.status in (SESSION_STOPPED, SESSION_FAILED):
        return row
    now = utcnow()
    started = _aware(row.started_at)
    if started is None:
        seconds = 0  # never ran: nothing billed
    else:
        end = now
        if reason == STOP_HOST_LOST and row.last_host_heartbeat_at:
            end = _aware(row.last_host_heartbeat_at)  # do not bill the customer for a vanished host
        seconds = int(max(0, min((end - started).total_seconds(), row.max_duration_seconds)))
        min_billable = int(settings.BILLING_MIN_BILLABLE_SECONDS or 0)
        seconds = max(seconds, min(min_billable, row.max_duration_seconds))
    captured = 0
    provider_minor = platform_minor = 0
    if seconds > 0 and row.rate_per_gpu_hour is not None:
        gpu_minor, fee_minor = await _price_parts(db, decimal.Decimal(row.rate_per_gpu_hour), row.gpu_count, seconds)
        total = gpu_minor + fee_minor
        captured = min(total, row.held_minor)
        platform_minor = min(fee_minor, captured)
        provider_minor = captured - platform_minor
    customer = await ledger.get_or_create_customer_account(db, row.customer_id)
    holding = await ledger.get_platform_account(db, "holding")
    platform_rev = await ledger.get_platform_account(db, "platform_revenue")
    if captured > 0:
        provider_acct = (
            await ledger.get_provider_account(db, row.provider_id) if row.provider_id else platform_rev
        )
        legs = [(holding, "debit", captured)]
        if provider_minor > 0:
            legs.append((provider_acct, "credit", provider_minor))
        if platform_minor > 0:
            legs.append((platform_rev, "credit", platform_minor))
        await ledger.post_transaction(
            db, kind="capture", legs=legs, idempotency_key=f"sess-capture:{row.id}",
            reference_type="session", reference_id=str(row.id), actor_user_id=row.customer_id,
        )
    remainder = row.held_minor - captured
    if remainder > 0:
        await ledger.post_transaction(
            db, kind="release", legs=[(holding, "debit", remainder), (customer, "credit", remainder)],
            idempotency_key=f"sess-release:{row.id}", reference_type="session", reference_id=str(row.id),
            actor_user_id=row.customer_id,
        )
    row.status = SESSION_FAILED if failed else SESSION_STOPPED
    row.stop_reason = reason
    row.stopped_at = now
    row.active_seconds = seconds
    row.captured_minor = captured
    # Kill the tokens so nothing can reconnect.
    row.host_token_hash = None
    row.browser_token_hash = None
    await db.flush()
    return row


def _sync_activity(relay):
    """Adapter for a relay object that only has the sync ``last_activity`` (older custom backends)."""

    async def _call(session_id: str):
        return relay.last_activity(session_id)

    return _call


async def reap(db: AsyncSession, relay=None) -> int:
    """Stop every live session past a deadline. Safe to call from the worker loop; also used lazily by the API."""
    now = utcnow()
    rows = (await db.execute(select(ComputeSession).where(ComputeSession.status.in_(SESSION_LIVE)))).scalars().all()
    n = 0
    for row in rows:
        act = (
            await (getattr(relay, "last_activity_async", None) or _sync_activity(relay))(str(row.id))
            if relay is not None else None
        )
        if act is not None:
            seen = dt.datetime.fromtimestamp(act, tz=dt.timezone.utc)
            cur = _aware(row.last_activity_at)
            if cur is None or seen > cur:
                row.last_activity_at = seen
        reason = deadline_reason(row, now)
        if reason:
            await stop_session(db, row, reason)
            if relay is not None:
                await relay.close_session(str(row.id), reason)
            n += 1
    return n


__all__ = [
    "SessionError", "create_session", "claim_next", "consume_token", "issue_browser_token", "refresh_host_token",
    "mark_active", "heartbeat", "stop_session", "reap", "deadline_reason", "hash_token", "validate_request",
    "STOP_HOST_ENDED",
]
