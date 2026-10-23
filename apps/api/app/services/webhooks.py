"""Outbound webhooks: SSRF-safe, signed, idempotent, retried (Phase B4).

Three properties this module exists to guarantee:

1. SSRF — a webhook URL is attacker-controlled by definition (a customer types
   it). Validating the *hostname* is not enough: ``evil.test`` can resolve to
   ``169.254.169.254`` and hand an attacker the cloud metadata service. So the
   host is RESOLVED and every returned address is checked against the private /
   loopback / link-local / reserved ranges, at registration AND again at every
   delivery attempt (DNS can be re-pointed after registration).
2. Idempotency — ``WebhookDelivery.delivery_key`` is UNIQUE and derived from
   (endpoint, event type, source object id). A replayed source event therefore
   enqueues at most one delivery, enforced by the database rather than by a
   pre-check that races.
3. Authenticity — each request carries ``X-ComputeCoin-Signature:
   sha256=<hex>``, an HMAC-SHA256 over ``"{timestamp}.{raw_body}"``. The
   timestamp is inside the signed string so a captured request cannot be
   replayed against the receiver outside its tolerance window.

HONEST NOTE ON THE SIGNING KEY: the schema stores only ``secret_hash``, so the
raw secret is not recoverable at delivery time. The signing key is therefore
*derived* from the secret (``sha256(secret)``) and that derivation is what is
persisted — a receiver who was shown the secret once can recompute it, and we
never store the secret itself. The residual property is that the stored digest
is signing-key-equivalent, so ``webhook_endpoints`` is secret-bearing data and
must be protected as such.

Outbound HTTP is injected (``sender``) so the test-suite runs fully offline.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import socket
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.webhooks import WebhookDelivery, WebhookEndpoint

__all__ = [
    "WEBHOOK_EVENTS",
    "SendResult",
    "WebhookUrlRejected",
    "generate_secret",
    "hash_secret",
    "compute_signature",
    "verify_signature",
    "validate_webhook_url",
    "enqueue_delivery",
    "dispatch_event",
    "attempt_delivery",
    "deliver_due",
    "signature_header_name",
]

# The event catalogue a customer may subscribe to.
WEBHOOK_EVENTS: tuple[str, ...] = (
    "job.completed",
    "job.failed",
    "balance.low",
    "invoice.paid",
)

SIGNATURE_HEADER = "X-ComputeCoin-Signature"
TIMESTAMP_HEADER = "X-ComputeCoin-Timestamp"
EVENT_HEADER = "X-ComputeCoin-Event"
DELIVERY_HEADER = "X-ComputeCoin-Delivery"

# Exponential backoff between attempts. Not settings-backed: the retry *count*
# is operator-tunable (WEBHOOK_MAX_ATTEMPTS), the curve is a protocol detail.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 3600.0

# A sink that fails this many times in a row is parked; a dead endpoint must not
# consume delivery capacity forever.
MAX_CONSECUTIVE_FAILURES = 20

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class WebhookUrlRejected(ValueError):
    """Raised when a URL is unusable or points somewhere we refuse to call."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SendResult:
    """Outcome of one outbound HTTP attempt."""

    status_code: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


# A sender takes (url, headers, raw_body) and returns a SendResult. Injected so
# tests never touch the network.
Sender = Callable[[str, dict[str, str], str], Awaitable[SendResult]]

# Resolver injection point: (host, port) -> list of IP strings.
Resolver = Callable[[str, int], Awaitable[list[str]]]


# ---------------------------------------------------------------------------
# Secrets & signing
# ---------------------------------------------------------------------------
def generate_secret() -> str:
    """A fresh signing secret, shown to the customer exactly once."""
    import secrets as _secrets

    return "whsec_" + _secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    """Derivation stored in ``webhook_endpoints.secret_hash`` (see module docstring)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def signing_payload(timestamp: str, raw_body: str) -> bytes:
    """The exact bytes that are signed: ``"{timestamp}.{raw_body}"``."""
    return f"{timestamp}.{raw_body}".encode("utf-8")


def _sign_with_key(signing_key: str, timestamp: str, raw_body: str) -> str:
    digest = hmac.new(
        signing_key.encode("utf-8"), signing_payload(timestamp, raw_body), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def compute_signature(secret: str, timestamp: str, raw_body: str) -> str:
    """Signature a receiver computes from the secret it was shown."""
    return _sign_with_key(hash_secret(secret), timestamp, raw_body)


def sign_stored(secret_hash: str, timestamp: str, raw_body: str) -> str:
    """Signature the platform computes from the stored derivation."""
    return _sign_with_key(secret_hash, timestamp, raw_body)


def verify_signature(secret: str, timestamp: str, raw_body: str, signature: str) -> bool:
    """Constant-time check used by tests and by any first-party receiver."""
    return hmac.compare_digest(compute_signature(secret, timestamp, raw_body), signature)


def signature_header_name() -> str:
    return SIGNATURE_HEADER


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------
async def _default_resolver(host: str, port: int) -> list[str]:
    """Resolve a host to every address family, off the event loop thread."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _address_is_forbidden(ip: ipaddress._BaseAddress) -> bool:
    """True for anything that is not a routable public address.

    Covers loopback (127./::1), RFC1918 private (10./172.16-31./192.168.),
    link-local INCLUDING the 169.254.169.254 cloud metadata endpoint, unique
    local v6, multicast, reserved and the unspecified address. IPv4-mapped v6
    (``::ffff:127.0.0.1``) is unwrapped first so it cannot smuggle a loopback
    past the check.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif getattr(ip, "sixtofour", None) is not None:
            ip = ip.sixtofour
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_webhook_url(url: str, *, resolver: Resolver | None = None) -> list[str]:
    """Refuse a URL we must not call; return the resolved addresses on success.

    Raises :class:`WebhookUrlRejected` with a stable ``code``:
    ``invalid_url``, ``unsupported_scheme``, ``url_has_credentials``,
    ``unresolvable_host`` or ``private_address_blocked``.
    """
    if not url or len(url) > 2048:
        raise WebhookUrlRejected("invalid_url", "empty or too long")

    try:
        parts = urlsplit(url)
    except ValueError as exc:  # pragma: no cover - urlsplit is very permissive
        raise WebhookUrlRejected("invalid_url", str(exc)) from exc

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        # file://, gopher://, ftp:// … all refused: only http(s) is deliverable.
        raise WebhookUrlRejected("unsupported_scheme", parts.scheme or "<none>")
    if parts.username or parts.password:
        # Credentials in the URL would be logged and leaked on redirect.
        raise WebhookUrlRejected("url_has_credentials")

    host = parts.hostname
    if not host:
        raise WebhookUrlRejected("invalid_url", "missing host")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise WebhookUrlRejected("invalid_url", "bad port") from exc

    if settings.WEBHOOK_ALLOW_PRIVATE_IPS:
        # Local development only; never enabled in a deployed environment.
        return []

    resolve = resolver or _default_resolver
    try:
        addresses = await resolve(host, port)
    except Exception as exc:
        raise WebhookUrlRejected("unresolvable_host", str(exc)[:200]) from exc
    if not addresses:
        raise WebhookUrlRejected("unresolvable_host", "no addresses")

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise WebhookUrlRejected("unresolvable_host", f"bad address {raw!r}")
        if _address_is_forbidden(ip):
            raise WebhookUrlRejected("private_address_blocked", str(ip))
    return [str(a) for a in addresses]


# ---------------------------------------------------------------------------
# Enqueue / dispatch
# ---------------------------------------------------------------------------
def build_delivery_key(endpoint_id: uuid.UUID, event_type: str, source_id: str) -> str:
    """Stable idempotency key for (endpoint, event, source object)."""
    return f"{endpoint_id}:{event_type}:{source_id}"


def _canonical_body(payload: dict) -> str:
    """Deterministic JSON. The signature covers these exact bytes, so the body
    sent on a retry must be byte-identical to the first attempt."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


async def enqueue_delivery(
    db: AsyncSession,
    endpoint: WebhookEndpoint,
    *,
    event_type: str,
    payload: dict,
    source_id: str,
    now: dt.datetime | None = None,
) -> tuple[WebhookDelivery, bool]:
    """Queue one delivery. Returns ``(delivery, created)``.

    The UNIQUE constraint on ``delivery_key`` decides; the INSERT runs in a
    SAVEPOINT so losing that race does not poison the caller's transaction.
    """
    key = build_delivery_key(endpoint.id, event_type, source_id)
    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_type=event_type,
        payload=payload,
        delivery_key=key,
        status="pending",
        next_attempt_at=ensure_aware(now) or utcnow(),
    )
    try:
        async with db.begin_nested():
            db.add(delivery)
            await db.flush()
    except IntegrityError:
        existing = (
            await db.execute(
                select(WebhookDelivery).where(WebhookDelivery.delivery_key == key)
            )
        ).scalar_one()
        return existing, False
    return delivery, True


async def dispatch_event(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    event_type: str,
    payload: dict,
    source_id: str,
    now: dt.datetime | None = None,
) -> list[WebhookDelivery]:
    """Fan one platform event out to the customer's subscribed, active sinks.

    Call sites hand a ``source_id`` that is stable for the event (e.g. the job
    id for ``job.completed``), so re-running the producer never double-delivers.
    """
    if event_type not in WEBHOOK_EVENTS:
        raise ValueError(f"unknown webhook event: {event_type!r}")

    endpoints = (
        await db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.customer_id == customer_id,
                WebhookEndpoint.active.is_(True),
            )
        )
    ).scalars().all()

    created: list[WebhookDelivery] = []
    for endpoint in endpoints:
        if event_type not in (endpoint.events or []):
            continue
        delivery, was_new = await enqueue_delivery(
            db,
            endpoint,
            event_type=event_type,
            payload=payload,
            source_id=source_id,
            now=now,
        )
        if was_new:
            created.append(delivery)
    return created


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def _pin_url(url: str, address: str) -> tuple[str, str]:
    """Rewrite ``url`` to dial ``address`` directly; return (url, Host header).

    The authority is replaced by the vetted IP while the original host:port is
    preserved in the ``Host`` header (and, for TLS, in the ``sni_hostname``
    request extension) so virtual hosting and certificate verification still
    work against the name the customer registered.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port
    authority_host = f"[{address}]" if ":" in address else address
    netloc = f"{authority_host}:{port}" if port else authority_host
    host_header = f"{host}:{port}" if port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")), host_header


def _pinned_sender(addresses: list[str]) -> Sender:
    """Build a sender that connects ONLY to an address already vetted.

    WHY: validating the hostname and then handing the URL to an HTTP client is a
    TOCTOU — the client performs its OWN DNS lookup, so a rebinding record (TTL 0
    alternating between a public IP and 169.254.169.254) passes the check and
    then delivers the signed POST to the metadata service. Pinning removes the
    second lookup entirely: the address that was checked is the address dialled.

    With ``WEBHOOK_ALLOW_PRIVATE_IPS`` (local dev) the validator returns no
    addresses and there is nothing to pin, so the plain sender is used.
    """
    if not addresses:
        return _httpx_sender

    async def _send(url: str, headers: dict[str, str], body: str) -> SendResult:
        import httpx

        parts = urlsplit(url)
        sni = parts.hostname or ""
        last_error: str | None = None
        for address in addresses:
            pinned_url, host_header = _pin_url(url, address)
            try:
                async with httpx.AsyncClient(
                    timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
                    follow_redirects=False,
                ) as http:
                    request = http.build_request(
                        "POST",
                        pinned_url,
                        headers={**headers, "Host": host_header},
                        content=body,
                        extensions={"sni_hostname": sni} if sni else None,
                    )
                    response = await http.send(request)
                return SendResult(status_code=response.status_code)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:500]
        return SendResult(status_code=None, error=last_error)

    return _send


async def _httpx_sender(url: str, headers: dict[str, str], body: str) -> SendResult:
    """Unpinned sender — used only when there is nothing to pin (dev mode)."""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
            # Never auto-follow: a 302 to 169.254.169.254 would bypass the
            # SSRF check we performed on the original URL.
            follow_redirects=False,
        ) as http:
            response = await http.post(url, headers=headers, content=body)
        return SendResult(status_code=response.status_code)
    except Exception as exc:
        return SendResult(status_code=None, error=f"{type(exc).__name__}: {exc}"[:500])


def backoff_seconds(attempts: int) -> float:
    """Delay before attempt ``attempts + 1`` — 2s, 4s, 8s … capped at an hour."""
    delay = BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0))
    return min(delay, BACKOFF_MAX_SECONDS)


async def attempt_delivery(
    db: AsyncSession,
    delivery: WebhookDelivery,
    *,
    sender: Sender | None = None,
    resolver: Resolver | None = None,
    now: dt.datetime | None = None,
) -> WebhookDelivery:
    """Make one signed attempt, then record success / schedule the retry.

    A delivery already ``delivered`` or ``failed`` is a no-op, so a double-tick
    of the dispatcher cannot re-send.
    """
    if delivery.status != "pending":
        return delivery

    moment = ensure_aware(now) or utcnow()
    endpoint = (
        await db.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.id == delivery.endpoint_id)
        )
    ).scalar_one()

    delivery.attempts += 1
    max_attempts = max(int(settings.WEBHOOK_MAX_ATTEMPTS), 1)

    def _fail(error: str, status_code: int | None = None) -> None:
        delivery.last_error = error[:500]
        delivery.response_status = status_code
        if delivery.attempts >= max_attempts:
            delivery.status = "failed"
        else:
            delivery.next_attempt_at = moment + dt.timedelta(
                seconds=backoff_seconds(delivery.attempts)
            )
        endpoint.failure_count += 1
        endpoint.last_error = error[:500]
        if endpoint.failure_count >= MAX_CONSECUTIVE_FAILURES:
            endpoint.active = False

    # Re-validate at send time: DNS may have been re-pointed since registration.
    # The vetted addresses are KEPT and pinned below — re-validating and then
    # letting the HTTP client resolve again is the TOCTOU this is meant to close.
    try:
        vetted = await validate_webhook_url(endpoint.url, resolver=resolver)
    except WebhookUrlRejected as exc:
        # Not a transient failure — never retried, and the sink is disabled.
        delivery.status = "failed"
        delivery.last_error = f"ssrf_blocked:{exc.code}"
        endpoint.active = False
        endpoint.last_error = f"ssrf_blocked:{exc.code}"
        await db.flush()
        return delivery

    body = _canonical_body(delivery.payload or {})
    timestamp = str(int(moment.timestamp()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ComputeCoin-Webhooks/1",
        EVENT_HEADER: delivery.event_type,
        DELIVERY_HEADER: str(delivery.id),
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign_stored(endpoint.secret_hash, timestamp, body),
    }

    send = sender or _pinned_sender(vetted)
    result = await send(endpoint.url, headers, body)

    if result.ok:
        delivery.status = "delivered"
        delivery.response_status = result.status_code
        delivery.delivered_at = moment
        delivery.last_error = None
        endpoint.failure_count = 0
        endpoint.last_error = None
    else:
        _fail(result.error or f"http_status:{result.status_code}", result.status_code)

    await db.flush()
    return delivery


async def deliver_due(
    db: AsyncSession,
    *,
    sender: Sender | None = None,
    resolver: Resolver | None = None,
    now: dt.datetime | None = None,
    limit: int = 100,
) -> list[WebhookDelivery]:
    """Attempt every pending delivery whose backoff has elapsed."""
    moment = ensure_aware(now) or utcnow()
    rows: Sequence[WebhookDelivery] = (
        await db.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_attempt_at <= moment,
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
            # Claim each row: every API worker runs its own sweeper, and an
            # unlocked SELECT lets two of them both see "pending" and both POST
            # the signed event. The delivery_key UNIQUE constraint makes ENQUEUE
            # idempotent; it does nothing for DELIVERY.
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    return [
        await attempt_delivery(
            db, delivery, sender=sender, resolver=resolver, now=moment
        )
        for delivery in rows
    ]
