"""WP-A: attestation challenge nonces expire, on both attestation routes.

The provider-login route (``/provider/machines/{id}/attestation/*``) used to accept a nonce of any
age. Both routes now share ``app.services.attestation_nonce``; these tests pin the rules: a fresh
nonce works, and an expired, undated, future-dated, wrong or replayed nonce is refused.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.base import utcnow
from app.models.enums import Role
from app.models.provider import ProviderMachine
from app.services.attestation_nonce import (
    NONCE_CREATED_AT_KEY,
    NONCE_KEY,
    NONCE_MAX_AGE_SECONDS,
    consume_attestation_nonce,
    issue_attestation_nonce,
    require_fresh_attestation_nonce,
)
from tests.test_agent_attestation import _agent_headers, _setup_machine
from tests.test_phase8_isolation import _create_test_user, _setup_provider_machine

SIGNING_KEY = "nonce-test-hw-key"
PCRS = {"pcr0": "a" * 64, "pcr1": "b" * 64}


# --------------------------------------------------------------------------- #
# Helper unit tests
# --------------------------------------------------------------------------- #
def _assert_refused(health: dict, nonce: str, now: dt.datetime) -> None:
    with pytest.raises(HTTPException) as exc:
        require_fresh_attestation_nonce(health, nonce, now=now)
    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid_or_expired_nonce"


def test_fresh_nonce_accepted_until_its_lifetime_ends():
    now = utcnow()
    health = issue_attestation_nonce({}, "n" * 48, now=now)
    require_fresh_attestation_nonce(health, "n" * 48, now=now)
    require_fresh_attestation_nonce(health, "n" * 48, now=now + dt.timedelta(seconds=NONCE_MAX_AGE_SECONDS))


def test_nonce_older_than_lifetime_refused():
    now = utcnow()
    health = issue_attestation_nonce({}, "n" * 48, now=now - dt.timedelta(seconds=NONCE_MAX_AGE_SECONDS + 1))
    _assert_refused(health, "n" * 48, now)


@pytest.mark.parametrize("created_at", [None, "", "not-a-date", 12345])
def test_missing_or_bad_creation_time_refused(created_at):
    health = {NONCE_KEY: "n" * 48}
    if created_at is not None:
        health[NONCE_CREATED_AT_KEY] = created_at
    _assert_refused(health, "n" * 48, utcnow())


def test_future_creation_time_refused():
    now = utcnow()
    health = issue_attestation_nonce({}, "n" * 48, now=now + dt.timedelta(minutes=5))
    _assert_refused(health, "n" * 48, now)


@pytest.mark.parametrize("presented", ["m" * 48, "", "ñ" * 24])
def test_wrong_nonce_refused_without_crashing(presented):
    health = issue_attestation_nonce({}, "n" * 48, now=utcnow())
    _assert_refused(health, presented, utcnow())


def test_consumed_nonce_refused_and_timestamp_cleared():
    now = utcnow()
    health = consume_attestation_nonce(issue_attestation_nonce({"other": 1}, "n" * 48, now=now))
    assert health == {"other": 1}
    _assert_refused(health, "n" * 48, now)


# --------------------------------------------------------------------------- #
# Provider-login route
# --------------------------------------------------------------------------- #
def _signed_quote(machine_id: uuid.UUID, nonce: str) -> dict:
    """Build a genuinely signed 'confidential' quote, so only the nonce decides the outcome."""
    quote_hasher = hashlib.sha384()
    quote_hasher.update(nonce.encode("utf-8"))
    for k in sorted(PCRS):
        quote_hasher.update(f"{k}:{PCRS[k]}".encode("utf-8"))
    platform_quote = quote_hasher.hexdigest()
    ts = utcnow().isoformat()
    canonical = (
        f"{machine_id}\nconfidential\n{nonce}\n{ts}\n{json.dumps(PCRS, sort_keys=True)}\n{platform_quote}"
    )
    sig = hmac.new(SIGNING_KEY.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "nonce": nonce,
        "isolation_tier": "confidential",
        "timestamp": ts,
        "pcr_measurements": PCRS,
        "platform_quote": platform_quote,
        "signature": sig,
    }


async def _provider_challenge(client: AsyncClient, db_engine) -> tuple[ProviderMachine, str, str]:
    user, token = await _create_test_user(db_engine, f"nonce-{uuid.uuid4().hex[:6]}@test.io", Role.provider)
    _, machine = await _setup_provider_machine(
        db_engine, user, "node-nonce", isolation_tiers=["container"], signing_key=SIGNING_KEY
    )
    resp = await client.post(
        f"/provider/machines/{machine.id}/attestation/challenge",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return machine, token, resp.json()["nonce"]


async def _set_health(db_engine, machine_id: uuid.UUID, mutate) -> None:
    async with async_sessionmaker(bind=db_engine, expire_on_commit=False)() as db:
        machine = await db.get(ProviderMachine, machine_id)
        health = dict(machine.last_health or {})
        mutate(health)
        machine.last_health = health
        await db.commit()


async def _provider_verify(client: AsyncClient, machine: ProviderMachine, token: str, nonce: str):
    return await client.post(
        f"/provider/machines/{machine.id}/attestation/verify",
        headers={"Authorization": f"Bearer {token}"},
        json=_signed_quote(machine.id, nonce),
    )


@pytest.mark.asyncio
async def test_provider_route_challenge_records_creation_time(client: AsyncClient, db_engine):
    machine, _, nonce = await _provider_challenge(client, db_engine)
    async with async_sessionmaker(bind=db_engine, expire_on_commit=False)() as db:
        health = (await db.get(ProviderMachine, machine.id)).last_health
    assert health[NONCE_KEY] == nonce
    created = dt.datetime.fromisoformat(health[NONCE_CREATED_AT_KEY])
    assert abs((utcnow() - created).total_seconds()) < 60


@pytest.mark.asyncio
async def test_provider_route_fresh_nonce_works_then_replay_refused(client: AsyncClient, db_engine):
    machine, token, nonce = await _provider_challenge(client, db_engine)
    resp = await _provider_verify(client, machine, token, nonce)
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"] is True

    replay = await _provider_verify(client, machine, token, nonce)
    assert replay.status_code == 400
    assert replay.json()["detail"] == "invalid_or_expired_nonce"


@pytest.mark.asyncio
async def test_provider_route_expired_nonce_refused(client: AsyncClient, db_engine):
    machine, token, nonce = await _provider_challenge(client, db_engine)
    backdated = (utcnow() - dt.timedelta(seconds=NONCE_MAX_AGE_SECONDS + 5)).isoformat()
    await _set_health(db_engine, machine.id, lambda h: h.__setitem__(NONCE_CREATED_AT_KEY, backdated))

    resp = await _provider_verify(client, machine, token, nonce)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_or_expired_nonce"


@pytest.mark.asyncio
async def test_provider_route_missing_creation_time_refused(client: AsyncClient, db_engine):
    machine, token, nonce = await _provider_challenge(client, db_engine)
    await _set_health(db_engine, machine.id, lambda h: h.pop(NONCE_CREATED_AT_KEY))

    resp = await _provider_verify(client, machine, token, nonce)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_or_expired_nonce"


# --------------------------------------------------------------------------- #
# Machine-HMAC route: same helper, same rule
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_agent_route_expired_nonce_refused(client: AsyncClient, db_engine):
    machine = await _setup_machine(db_engine, signing_key=SIGNING_KEY)
    resp = await client.post(
        "/v1/agents/attestation/challenge", content=b"{}", headers=_agent_headers(machine.id, SIGNING_KEY, b"{}")
    )
    assert resp.status_code == 200, resp.text
    nonce = resp.json()["nonce"]
    backdated = (utcnow() - dt.timedelta(seconds=NONCE_MAX_AGE_SECONDS + 5)).isoformat()
    await _set_health(db_engine, machine.id, lambda h: h.__setitem__(NONCE_CREATED_AT_KEY, backdated))

    raw = json.dumps(_signed_quote(machine.id, nonce), sort_keys=True, separators=(",", ":")).encode("utf-8")
    resp = await client.post(
        "/v1/agents/attestation/verify", content=raw, headers=_agent_headers(machine.id, SIGNING_KEY, raw)
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_or_expired_nonce"
