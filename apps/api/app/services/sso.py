"""Enterprise SSO (OIDC authorization-code flow) for organizations — Phase C2.

Secret handling
---------------
``SsoConnection`` stores **no client secret**. It stores

* ``config["client_secret_ref"]`` — the *name* of an entry in the deployment's
  secret manager, and
* ``config["client_secret_sha256"]`` — a fingerprint of the value that was in
  the secret manager when the connection was configured.

The real secret is fetched at flow time through a :class:`SecretResolver`. The
default resolver reads ``SSO_CLIENT_SECRET__<REF>`` from the process
environment, which is how secrets reach the container in this deployment
(Kubernetes/Fly secrets land as env vars); swapping in a Vault/AWS Secrets
Manager resolver is a one-line injection. Storing the plaintext in Postgres was
rejected: the token endpoint needs the live value, so a DB leak would otherwise
hand an attacker a working IdP client. The fingerprint lets us detect an
out-of-band rotation and fail loudly instead of looping on invalid_client.

Testability
-----------
Every outbound call goes through the :class:`OidcClient` protocol, injected as a
FastAPI dependency, so the whole flow runs offline against a mock IdP. The
fixture-free HTTP implementation is SSRF-guarded: IdP endpoints are
administrator-supplied URLs, i.e. user input.

Honest limitation: a mocked IdP proves our side of the protocol (state/nonce/
issuer/audience/signature checks, JIT provisioning). It does not prove
interoperability with a specific real IdP (Okta, Entra ID, Google) — that needs
a staging IdP and is explicitly out of scope of the automated suite.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
import secrets
import socket
import uuid
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import HTTPException, status
from jwt import PyJWK

from app.core.config import settings

STATE_PURPOSE = "sso_oidc_state"
#: An authorization round-trip is interactive; 10 minutes is generous and keeps
#: a leaked state parameter useless shortly after.
STATE_TTL_SECONDS = 600

#: Only symmetric HS* algorithms are usable without the optional `cryptography`
#: package. RS*/ES* are accepted here and verified properly when the package is
#: installed; otherwise the flow fails closed (never "skip verification").
ALLOWED_ID_TOKEN_ALGS = ("RS256", "RS384", "RS512", "ES256", "HS256", "HS384", "HS512")


class SsoError(HTTPException):
    """A 400-class SSO failure with a stable machine-readable detail code."""

    def __init__(self, code: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> None:
        super().__init__(status_code=status_code, detail=code)


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------
class SecretResolver(Protocol):
    def resolve(self, ref: str) -> str | None:  # pragma: no cover - protocol
        ...


class EnvSecretResolver:
    """Reads ``SSO_CLIENT_SECRET__<REF>`` from the environment.

    The ref is upper-cased and non-alphanumerics become ``_`` so that a ref like
    ``okta-prod`` maps to ``SSO_CLIENT_SECRET__OKTA_PROD``.
    """

    prefix = "SSO_CLIENT_SECRET__"

    def env_name(self, ref: str) -> str:
        sanitized = "".join(ch if ch.isalnum() else "_" for ch in ref).upper()
        return f"{self.prefix}{sanitized}"

    def resolve(self, ref: str) -> str | None:
        return os.environ.get(self.env_name(ref))


_secret_resolver: SecretResolver = EnvSecretResolver()


def get_secret_resolver() -> SecretResolver:
    """FastAPI dependency: the deployment's secret-manager adapter."""
    return _secret_resolver


def fingerprint_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def resolve_client_secret(config: dict[str, Any], resolver: SecretResolver) -> str:
    """Fetch the live client secret, refusing to proceed on drift."""
    ref = str(config.get("client_secret_ref") or "")
    if not ref:
        raise SsoError("sso_client_secret_not_configured", status.HTTP_503_SERVICE_UNAVAILABLE)
    secret = resolver.resolve(ref)
    if not secret:
        raise SsoError("sso_client_secret_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
    expected = config.get("client_secret_sha256")
    if expected and not secrets.compare_digest(fingerprint_secret(secret), str(expected)):
        # The secret manager was rotated without re-registering the connection.
        raise SsoError("sso_client_secret_rotated", status.HTTP_503_SERVICE_UNAVAILABLE)
    return secret


# ---------------------------------------------------------------------------
# SSRF guard for admin-supplied IdP endpoints
# ---------------------------------------------------------------------------
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return True
    return any(addr in net for net in _BLOCKED_NETWORKS)


def assert_public_http_url(url: str) -> None:
    """Reject non-http(s) schemes and hosts resolving into private space.

    Organization admins supply these endpoints, so they are user input: without
    this check an admin could point ``token_endpoint`` at ``http://169.254.169.254``
    and have the API relay cloud instance credentials.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SsoError("sso_endpoint_scheme_not_allowed")
    host = parsed.hostname
    if not host:
        raise SsoError("sso_endpoint_invalid")
    if settings.WEBHOOK_ALLOW_PRIVATE_IPS:
        # Shared "allow private egress" switch used by the dev stack.
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise SsoError("sso_endpoint_unresolvable")
    for info in infos:
        if _is_blocked_ip(info[4][0]):
            raise SsoError("sso_endpoint_private_address")


# ---------------------------------------------------------------------------
# Injectable OIDC transport
# ---------------------------------------------------------------------------
class OidcClient(Protocol):
    async def exchange_code(
        self, token_endpoint: str, data: dict[str, str]
    ) -> dict[str, Any]:  # pragma: no cover - protocol
        ...

    async def fetch_jwks(self, jwks_uri: str) -> dict[str, Any]:  # pragma: no cover
        ...

    async def discover(self, issuer: str) -> dict[str, Any]:  # pragma: no cover
        ...


class HttpxOidcClient:
    """Real transport: SSRF-guarded, short-timeout httpx calls."""

    def __init__(self, timeout: float | None = None) -> None:
        self._timeout = timeout if timeout is not None else settings.WEBHOOK_TIMEOUT_SECONDS

    async def exchange_code(self, token_endpoint: str, data: dict[str, str]) -> dict[str, Any]:
        assert_public_http_url(token_endpoint)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(token_endpoint, data=data)
        if resp.status_code != 200:
            raise SsoError("sso_token_exchange_failed")
        return resp.json()

    async def fetch_jwks(self, jwks_uri: str) -> dict[str, Any]:
        assert_public_http_url(jwks_uri)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(jwks_uri)
        if resp.status_code != 200:
            raise SsoError("sso_jwks_fetch_failed")
        return resp.json()

    async def discover(self, issuer: str) -> dict[str, Any]:
        url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        assert_public_http_url(url)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            raise SsoError("sso_discovery_failed")
        return resp.json()


def get_oidc_client() -> OidcClient:
    """FastAPI dependency; tests override this with a mock IdP."""
    return HttpxOidcClient()


# ---------------------------------------------------------------------------
# State / nonce
# ---------------------------------------------------------------------------
def issue_state(connection_id: uuid.UUID) -> tuple[str, str]:
    """Return ``(state, nonce)``.

    The state is a short-lived JWT signed with ``SESSION_SECRET`` rather than a
    DB row: it is self-contained, so a forged or tampered state fails the
    signature check and no server-side cleanup job is needed. The nonce travels
    *inside* the signed state, which is what lets the callback detect an
    id_token minted for a different authorization request (replay).
    """
    now = dt.datetime.now(dt.timezone.utc)
    nonce = secrets.token_urlsafe(24)
    payload = {
        "purpose": STATE_PURPOSE,
        "cid": str(connection_id),
        "nonce": nonce,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=STATE_TTL_SECONDS)).timestamp()),
        "jti": secrets.token_urlsafe(12),
    }
    state = jwt.encode(payload, settings.SESSION_SECRET, algorithm="HS256")
    return state, nonce


def verify_state(state: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            state,
            settings.SESSION_SECRET,
            algorithms=["HS256"],
            options={"require": ["exp", "jti"]},
        )
    except jwt.PyJWTError:
        raise SsoError("sso_invalid_state")
    if payload.get("purpose") != STATE_PURPOSE or not payload.get("nonce"):
        raise SsoError("sso_invalid_state")
    return payload


def build_authorization_url(
    *, config: dict[str, Any], client_id: str, state: str, nonce: str
) -> str:
    """Compose the IdP authorization URL.

    ``redirect_uri`` comes from the stored connection, never from the caller —
    accepting a caller-supplied redirect would make this an open redirector and
    leak authorization codes.
    """
    authorization_endpoint = str(config.get("authorization_endpoint") or "")
    redirect_uri = str(config.get("redirect_uri") or "")
    if not authorization_endpoint or not redirect_uri:
        raise SsoError("sso_connection_incomplete", status.HTTP_503_SERVICE_UNAVAILABLE)
    assert_public_http_url(authorization_endpoint)
    scopes = config.get("scopes") or ["openid", "email"]
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "nonce": nonce,
        }
    )
    separator = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{separator}{query}"


# ---------------------------------------------------------------------------
# id_token verification
# ---------------------------------------------------------------------------
def _key_for(jwks: dict[str, Any], token: str, client_secret: str) -> tuple[Any, str]:
    """Resolve the verification key for ``token`` from the IdP's JWKS.

    Symmetric (``oct``) keys fall back to the client secret, which is what
    HS256-signing IdPs use. Returns ``(key, alg)``.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise SsoError("sso_invalid_id_token")
    alg = header.get("alg")
    if alg not in ALLOWED_ID_TOKEN_ALGS:
        raise SsoError("sso_id_token_alg_not_allowed")
    kid = header.get("kid")
    for entry in jwks.get("keys", []) or []:
        if kid and entry.get("kid") != kid:
            continue
        # Algorithm-confusion guard: the key family decides which algorithms are
        # acceptable, never the attacker-controlled JOSE header. Without this, an
        # RSA verification key could be replayed as an HS256 *shared secret* (the
        # public key is public), forging arbitrary id_tokens.
        kty = str(entry.get("kty") or "").upper()
        allowed_prefix = {"RSA": "RS", "EC": "ES", "OCT": "HS"}.get(kty)
        if allowed_prefix is None or not str(alg).startswith(allowed_prefix):
            raise SsoError("sso_id_token_alg_not_allowed")
        entry_alg = entry.get("alg")
        if entry_alg and entry_alg != alg:
            # The IdP pinned an algorithm for this key; honour it strictly.
            raise SsoError("sso_id_token_alg_not_allowed")
        try:
            return PyJWK.from_dict({**entry, "alg": alg}).key, alg
        except SsoError:
            raise
        except Exception:
            # e.g. an RSA key while `cryptography` is unavailable: fail closed.
            raise SsoError("sso_id_token_key_unsupported", status.HTTP_503_SERVICE_UNAVAILABLE)
    if str(alg).startswith("HS"):
        # No JWKS entry matched: fall back to the client secret, which is what
        # HS256-signing IdPs use. Asymmetric algs have no such fallback.
        return client_secret.encode("utf-8"), alg
    raise SsoError("sso_id_token_key_not_found")


def verify_id_token(
    *,
    id_token: str,
    jwks: dict[str, Any],
    client_secret: str,
    issuer: str,
    client_id: str,
    nonce: str,
) -> dict[str, Any]:
    """Validate signature, issuer, audience, expiry and nonce. Fails closed."""
    key, alg = _key_for(jwks, id_token, client_secret)
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=[alg],
            audience=client_id,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.InvalidIssuerError:
        raise SsoError("sso_id_token_issuer_mismatch")
    except jwt.InvalidAudienceError:
        raise SsoError("sso_id_token_audience_mismatch")
    except jwt.ExpiredSignatureError:
        raise SsoError("sso_id_token_expired")
    except jwt.PyJWTError:
        raise SsoError("sso_invalid_id_token")

    token_nonce = claims.get("nonce")
    if not token_nonce or not secrets.compare_digest(str(token_nonce), nonce):
        raise SsoError("sso_nonce_mismatch")
    return claims


def extract_email(claims: dict[str, Any]) -> str:
    """Pull a usable, IdP-verified email out of the id_token claims.

    We refuse ``email_verified: false``: provisioning an account from an
    unverified IdP email would let an attacker pre-claim someone's mailbox.
    """
    email = claims.get("email")
    if not email or not isinstance(email, str) or "@" not in email:
        raise SsoError("sso_email_claim_missing")
    if claims.get("email_verified") is False:
        raise SsoError("sso_email_not_verified")
    return email.strip().lower()
