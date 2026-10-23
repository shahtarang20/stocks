"""Phase C3 — provider pricing controls + Stripe Connect payouts.

Proves (each test fails without the C3 code):

Pricing controls
1. The floor price is enforced at write time: an asking price below the floor is
   refused (422) and a floor above the current asking price is refused too.
2. A machine whose asking price sits below its own floor is INELIGIBLE for the
   scheduler (``is_machine_eligible_now`` is False, reason ``price_below_floor``).
3. Outside every availability window -> ineligible; inside one -> eligible.
4. A model absent from ``model_allowlist`` -> ineligible; a listed one -> eligible.
5. Malformed policy (bad time, bad weekday, zero/negative floor) -> 422.
6. Another provider editing/reading my machine gets 404, never 403.

Connect payouts
7. Onboarding creates exactly one ``connect_accounts`` row and is idempotent.
8. No payout without Stripe's KYC verdict (``payouts_enabled``).
9. A payout larger than available earnings is refused and moves no money.
10. A replayed payout (same idempotency key) pays exactly once — one Stripe
    transfer, one ledger request transaction, one ``provider_payouts`` row.
11. Webhook signatures are verified: a valid ``account.updated`` enables payouts,
    a forged signature is rejected (400) and changes nothing.
12. The double-entry ledger stays balanced across all of the above.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import time
import uuid

import pytest
import stripe
from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.main import app
from app.models.billing import ProviderPayout
from app.models.enterprise import ConnectAccount
from app.models.provider import Provider, ProviderMachine
from app.services import ledger
from app.services.connect_payouts import get_connect_client
from app.services.provider_pricing import (
    is_machine_eligible_now,
    machine_ineligibility_reason,
)
from tests.helpers import auth_headers, get_user_id
from tests.test_phase3_marketplace import create_listable_machine

pytestmark = pytest.mark.asyncio

TEST_WEBHOOK_SECRET = "whsec_connect_test_secret_abcdef"


# ---------------------------------------------------------------------------
# Router wiring for the test session (the Integrator wires app/main.py; until
# then mount the C3 routers here so the suite exercises the real app).
# ---------------------------------------------------------------------------
def _ensure_routers_mounted() -> None:
    paths = {getattr(r, "path", "") for r in app.routes}
    if "/v1/provider/machines/{machine_id}/pricing" not in paths:
        from app.api.routers import provider_pricing as _pricing

        app.include_router(_pricing.router)
    if "/v1/provider/connect/onboarding" not in paths:
        from app.api.routers import connect as _connect

        app.include_router(_connect.router)


_ensure_routers_mounted()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class FakeConnectClient:
    """Injected stand-in for Stripe Connect — counts calls, never touches network."""

    mode = "test"

    def __init__(self, *, payouts_enabled: bool = False) -> None:
        self.payouts_enabled = payouts_enabled
        self.transfers: list[dict] = []
        self.accounts_created = 0

    def create_account(self, *, provider_id: str, email: str | None) -> dict:
        self.accounts_created += 1
        return {
            "id": f"acct_test_{uuid.uuid4().hex[:16]}",
            "payouts_enabled": self.payouts_enabled,
            "details_submitted": self.payouts_enabled,
            "requirements": {"currently_due": [] if self.payouts_enabled else ["external_account"]},
        }

    def create_account_link(self, *, account_id: str, refresh_url: str, return_url: str) -> dict:
        return {"url": f"https://connect.test/{account_id}", "expires_at": 1700000000}

    def retrieve_account(self, *, account_id: str) -> dict:
        return {
            "id": account_id,
            "payouts_enabled": self.payouts_enabled,
            "details_submitted": self.payouts_enabled,
            "requirements": {"currently_due": []},
        }

    def create_transfer(
        self, *, amount_minor: int, currency: str, destination: str, idempotency_key: str, metadata: dict
    ) -> dict:
        self.transfers.append(
            {
                "amount": amount_minor,
                "destination": destination,
                "idempotency_key": idempotency_key,
                "metadata": metadata,
            }
        )
        return {"id": f"tr_test_{len(self.transfers)}"}


@pytest.fixture
def fake_connect():
    fake = FakeConnectClient()
    app.dependency_overrides[get_connect_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_connect_client, None)


async def _provider_id(email: str) -> uuid.UUID:
    user_id = await get_user_id(email)
    sm = get_sessionmaker()
    async with sm() as s:
        return (
            await s.execute(select(Provider).where(Provider.user_id == user_id))
        ).scalar_one().id


async def _load_machine(machine_id: str) -> ProviderMachine:
    sm = get_sessionmaker()
    async with sm() as s:
        return (
            await s.execute(
                select(ProviderMachine).where(ProviderMachine.id == uuid.UUID(machine_id))
            )
        ).scalar_one()


async def _set_asking_price(machine_id: str, price: str | None) -> None:
    """Force the asking price directly (simulating later drift below the floor)."""
    sm = get_sessionmaker()
    async with sm() as s:
        machine = (
            await s.execute(
                select(ProviderMachine).where(ProviderMachine.id == uuid.UUID(machine_id))
            )
        ).scalar_one()
        machine.asking_price_per_hour = None if price is None else decimal.Decimal(price)
        await s.commit()


async def _fund_earnings(email: str, amount: str = "100.00") -> uuid.UUID:
    """Credit a provider's earnings account THROUGH the ledger (never direct)."""
    provider_id = await _provider_id(email)
    sm = get_sessionmaker()
    async with sm() as s:
        earnings = await ledger.get_provider_account(s, provider_id)
        external = await ledger.get_platform_account(s, "external")
        minor = ledger.to_minor(decimal.Decimal(amount))
        await ledger.post_transaction(
            s,
            kind="job_settlement",
            legs=[(external, "debit", minor), (earnings, "credit", minor)],
            idempotency_key=f"test_fund:{provider_id}:{amount}",
            reference_type="test",
            reference_id=str(provider_id),
        )
        await s.commit()
    return provider_id


async def _assert_ledger_balanced() -> None:
    sm = get_sessionmaker()
    async with sm() as s:
        debits, credits = await ledger.ledger_totals(s)
    assert debits == credits, f"ledger imbalance: debits={debits} credits={credits}"


def _window_around_now(start_offset_h: int, end_offset_h: int) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    start = (now + dt.timedelta(hours=start_offset_h)).strftime("%H:%M")
    end = (now + dt.timedelta(hours=end_offset_h)).strftime("%H:%M")
    return {"days": list(range(7)), "start": start, "end": end, "tz": "UTC"}


def _stripe_sig(payload: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    return stripe.WebhookSignature.generate_signature_header(
        payload=payload.decode("utf-8"), secret=secret, timestamp=int(time.time())
    )


# ===========================================================================
# A. Pricing controls
# ===========================================================================
async def test_floor_price_write_time_enforcement(client: AsyncClient):
    """Floor above asking -> 422; asking below an existing floor -> 422."""
    m = await create_listable_machine(client, "c3_floor@example.com", price="1.50")
    mid, access = m["machine_id"], m["access"]
    url = f"/v1/provider/machines/{mid}/pricing"

    # Floor higher than the current asking price is refused.
    bad = await client.put(url, json={"floor_price_per_hour": "2.00"}, headers=auth_headers(access))
    assert bad.status_code == 422, bad.text
    assert bad.json()["detail"] == "asking_price_below_floor"

    # A floor at or below the asking price is accepted.
    ok = await client.put(url, json={"floor_price_per_hour": "1.00"}, headers=auth_headers(access))
    assert ok.status_code == 200, ok.text
    assert ok.json()["floor_price_per_hour"] == "1.00"
    assert ok.json()["eligible_now"] is True

    # Dropping the asking price below the now-stored floor is refused.
    drop = await client.put(
        url, json={"asking_price_per_hour": "0.50"}, headers=auth_headers(access)
    )
    assert drop.status_code == 422, drop.text
    assert drop.json()["detail"] == "asking_price_below_floor"

    # ...and nothing was persisted by the refused write.
    machine = await _load_machine(mid)
    assert decimal.Decimal(machine.asking_price_per_hour) == decimal.Decimal("1.50")


async def test_machine_below_its_floor_is_ineligible(client: AsyncClient):
    """Scheduler gate: asking price under the floor -> ineligible, with a reason."""
    m = await create_listable_machine(client, "c3_floor2@example.com", price="2.00")
    mid, access = m["machine_id"], m["access"]
    r = await client.put(
        f"/v1/provider/machines/{mid}/pricing",
        json={"floor_price_per_hour": "2.00"},
        headers=auth_headers(access),
    )
    assert r.status_code == 200, r.text

    machine = await _load_machine(mid)
    assert is_machine_eligible_now(machine) is True

    await _set_asking_price(mid, "1.00")
    machine = await _load_machine(mid)
    assert is_machine_eligible_now(machine) is False
    assert machine_ineligibility_reason(machine) == "price_below_floor"

    # The API reports the same verdict to the operator.
    read = await client.get(
        f"/v1/provider/machines/{mid}/pricing", headers=auth_headers(access)
    )
    assert read.status_code == 200
    assert read.json()["eligible_now"] is False
    assert read.json()["ineligible_reason"] == "price_below_floor"


async def test_availability_window_gates_eligibility(client: AsyncClient):
    """Inside a window -> eligible; outside every window -> ineligible."""
    m = await create_listable_machine(client, "c3_window@example.com", price="1.50")
    mid, access = m["machine_id"], m["access"]
    url = f"/v1/provider/machines/{mid}/pricing"

    inside = await client.put(
        url, json={"availability_windows": [_window_around_now(-1, 1)]}, headers=auth_headers(access)
    )
    assert inside.status_code == 200, inside.text
    assert inside.json()["eligible_now"] is True
    assert is_machine_eligible_now(await _load_machine(mid)) is True

    outside = await client.put(
        url, json={"availability_windows": [_window_around_now(2, 3)]}, headers=auth_headers(access)
    )
    assert outside.status_code == 200, outside.text
    assert outside.json()["eligible_now"] is False
    assert outside.json()["ineligible_reason"] == "outside_availability_window"

    machine = await _load_machine(mid)
    assert is_machine_eligible_now(machine) is False
    assert machine_ineligibility_reason(machine) == "outside_availability_window"

    # Clearing the schedule restores "always available".
    cleared = await client.put(
        url, json={"availability_windows": None}, headers=auth_headers(access)
    )
    assert cleared.status_code == 200
    assert cleared.json()["eligible_now"] is True


async def test_model_allowlist_gates_eligibility(client: AsyncClient):
    m = await create_listable_machine(client, "c3_models@example.com", price="1.50")
    mid, access = m["machine_id"], m["access"]
    url = f"/v1/provider/machines/{mid}/pricing"

    r = await client.put(
        url, json={"model_allowlist": ["tinyllama-1.1b-chat-v1"]}, headers=auth_headers(access)
    )
    assert r.status_code == 200, r.text

    machine = await _load_machine(mid)
    assert is_machine_eligible_now(machine, "tinyllama-1.1b-chat-v1") is True
    assert is_machine_eligible_now(machine, "some-other-model-v9") is False
    assert machine_ineligibility_reason(machine, "some-other-model-v9") == "model_not_allowlisted"
    # A machine that restricts models cannot serve an unspecified model.
    assert is_machine_eligible_now(machine, None) is False

    allowed = await client.get(
        f"{url}?model=tinyllama-1.1b-chat-v1", headers=auth_headers(access)
    )
    assert allowed.json()["eligible_now"] is True
    denied = await client.get(f"{url}?model=some-other-model-v9", headers=auth_headers(access))
    assert denied.json()["eligible_now"] is False
    assert denied.json()["ineligible_reason"] == "model_not_allowlisted"


@pytest.mark.parametrize(
    "payload,detail",
    [
        ({"floor_price_per_hour": "0"}, "floor_price_must_be_positive"),
        ({"floor_price_per_hour": "-1.00"}, "floor_price_must_be_positive"),
        ({"availability_windows": [{"start": "25:00", "end": "06:00"}]}, "window_bad_start"),
        ({"availability_windows": [{"start": "18:00", "end": "18:00"}]}, "window_zero_length"),
        ({"availability_windows": [{"start": "18:00", "end": "06:00", "days": [9]}]}, "window_bad_days"),
        ({"availability_windows": [{"start": "18:00", "end": "06:00", "tz": "Mars/Olympus"}]}, "window_bad_tz"),
        ({"model_allowlist": [""]}, "allowlist_entry_invalid"),
    ],
)
async def test_invalid_policy_rejected(client: AsyncClient, payload: dict, detail: str):
    m = await create_listable_machine(
        client, f"c3_bad_{abs(hash(detail)) % 10_000}@example.com", price="1.50"
    )
    r = await client.put(
        f"/v1/provider/machines/{m['machine_id']}/pricing",
        json=payload,
        headers=auth_headers(m["access"]),
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == detail


async def test_other_provider_cannot_touch_my_machine(client: AsyncClient):
    """Cross-tenant access returns 404 (not 403) and changes nothing."""
    mine = await create_listable_machine(client, "c3_owner@example.com", price="1.50")
    other = await create_listable_machine(
        client, "c3_intruder@example.com", price="1.50", hostname="intruder-box"
    )
    mid = mine["machine_id"]

    read = await client.get(
        f"/v1/provider/machines/{mid}/pricing", headers=auth_headers(other["access"])
    )
    assert read.status_code == 404, read.text

    write = await client.put(
        f"/v1/provider/machines/{mid}/pricing",
        json={"floor_price_per_hour": "0.01"},
        headers=auth_headers(other["access"]),
    )
    assert write.status_code == 404, write.text

    machine = await _load_machine(mid)
    assert machine.floor_price_per_hour is None

    # A bogus bearer token is rejected outright (401, not 404).
    # NOTE: the shared httpx client keeps session cookies, so an explicit
    # Authorization header is used to force the unauthenticated path.
    anon = await client.get(
        f"/v1/provider/machines/{mid}/pricing", headers=auth_headers("not-a-real-jwt")
    )
    assert anon.status_code == 401


# ===========================================================================
# B. Stripe Connect payouts
# ===========================================================================
async def test_onboarding_creates_single_connect_account(client: AsyncClient, fake_connect):
    m = await create_listable_machine(client, "c3_connect@example.com", price="1.50")
    access = m["access"]

    first = await client.post(
        "/v1/provider/connect/onboarding", json={}, headers=auth_headers(access)
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["created"] is True
    assert body["stripe_account_id"].startswith("acct_test_")
    assert body["onboarding_url"].startswith("https://connect.test/")
    assert body["payouts_enabled"] is False
    assert body["mode"] == "test"

    # Idempotent: a second call reuses the SAME Stripe account.
    second = await client.post(
        "/v1/provider/connect/onboarding", json={}, headers=auth_headers(access)
    )
    assert second.status_code == 201, second.text
    assert second.json()["created"] is False
    assert second.json()["stripe_account_id"] == body["stripe_account_id"]
    assert fake_connect.accounts_created == 1

    sm = get_sessionmaker()
    async with sm() as s:
        count = (
            await s.execute(select(func.count()).select_from(ConnectAccount))
        ).scalar_one()
    assert count == 1


async def test_onboarding_rejects_unsafe_redirect_url(client: AsyncClient, fake_connect):
    m = await create_listable_machine(client, "c3_redirect@example.com", price="1.50")
    r = await client.post(
        "/v1/provider/connect/onboarding",
        json={"return_url": "javascript:alert(1)"},
        headers=auth_headers(m["access"]),
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "invalid_redirect_url"


async def test_payout_refused_without_kyc(client: AsyncClient, fake_connect):
    """Stripe has not enabled payouts -> refuse, move no money, call no transfer."""
    m = await create_listable_machine(client, "c3_kyc@example.com", price="1.50")
    access = m["access"]
    await _fund_earnings("c3_kyc@example.com", "50.00")
    await client.post("/v1/provider/connect/onboarding", json={}, headers=auth_headers(access))

    r = await client.post(
        "/v1/provider/connect/payouts",
        json={"amount": "10.00", "idempotency_key": "payout-kyc-0001"},
        headers=auth_headers(access),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "connect_payouts_not_enabled"
    assert fake_connect.transfers == []

    provider_id = await _provider_id("c3_kyc@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        earnings = await ledger.get_provider_account(s, provider_id)
        assert earnings.balance_minor == 5000
    await _assert_ledger_balanced()


async def test_payout_beyond_available_earnings_refused(client: AsyncClient, fake_connect):
    m = await create_listable_machine(client, "c3_over@example.com", price="1.50")
    access = m["access"]
    await _fund_earnings("c3_over@example.com", "20.00")
    fake_connect.payouts_enabled = True
    await client.post("/v1/provider/connect/onboarding", json={}, headers=auth_headers(access))

    r = await client.post(
        "/v1/provider/connect/payouts",
        json={"amount": "20.01", "idempotency_key": "payout-over-0001"},
        headers=auth_headers(access),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "insufficient_earnings"
    assert fake_connect.transfers == []

    provider_id = await _provider_id("c3_over@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        earnings = await ledger.get_provider_account(s, provider_id)
        assert earnings.balance_minor == 2000
        payouts = (
            await s.execute(select(func.count()).select_from(ProviderPayout))
        ).scalar_one()
    assert payouts == 0
    await _assert_ledger_balanced()


async def test_payout_succeeds_and_is_idempotent_on_replay(client: AsyncClient, fake_connect):
    m = await create_listable_machine(client, "c3_payout@example.com", price="1.50")
    access = m["access"]
    provider_id = await _fund_earnings("c3_payout@example.com", "40.00")
    fake_connect.payouts_enabled = True
    await client.post("/v1/provider/connect/onboarding", json={}, headers=auth_headers(access))

    body = {"amount": "15.00", "idempotency_key": "payout-replay-0001"}
    first = await client.post(
        "/v1/provider/connect/payouts", json=body, headers=auth_headers(access)
    )
    assert first.status_code == 201, first.text
    data = first.json()
    assert data["amount"] == "15.00"
    assert data["status"] == "settled"
    assert data["replayed"] is False
    assert data["mode"] == "test"
    assert len(fake_connect.transfers) == 1
    assert fake_connect.transfers[0]["amount"] == 1500

    # Replay with the SAME key: same payout, no second transfer, no second debit.
    replay = await client.post(
        "/v1/provider/connect/payouts", json=body, headers=auth_headers(access)
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["payout_id"] == data["payout_id"]
    assert replay.json()["replayed"] is True
    assert len(fake_connect.transfers) == 1

    sm = get_sessionmaker()
    async with sm() as s:
        earnings = await ledger.get_provider_account(s, provider_id)
        assert earnings.balance_minor == 2500  # 40.00 - 15.00, debited exactly once
        payout_rows = (
            await s.execute(
                select(func.count()).select_from(ProviderPayout).where(
                    ProviderPayout.provider_id == provider_id
                )
            )
        ).scalar_one()
    assert payout_rows == 1
    await _assert_ledger_balanced()

    # A DIFFERENT key is a genuinely new payout.
    second = await client.post(
        "/v1/provider/connect/payouts",
        json={"amount": "5.00", "idempotency_key": "payout-replay-0002"},
        headers=auth_headers(access),
    )
    assert second.status_code == 201, second.text
    assert second.json()["payout_id"] != data["payout_id"]
    assert len(fake_connect.transfers) == 2
    await _assert_ledger_balanced()


async def test_connect_webhook_signature_is_verified(client: AsyncClient, fake_connect):
    """A signed account.updated enables payouts; a forged one changes nothing."""
    m = await create_listable_machine(client, "c3_hook@example.com", price="1.50")
    access = m["access"]
    onboard = await client.post(
        "/v1/provider/connect/onboarding", json={}, headers=auth_headers(access)
    )
    stripe_account_id = onboard.json()["stripe_account_id"]
    assert onboard.json()["payouts_enabled"] is False

    event = {
        "id": f"evt_{uuid.uuid4().hex[:16]}",
        "type": "account.updated",
        "data": {
            "object": {
                "id": stripe_account_id,
                "payouts_enabled": True,
                "details_submitted": True,
                "requirements": {"currently_due": []},
            }
        },
    }
    payload = json.dumps(event).encode()

    import unittest.mock as _mock

    with _mock.patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        # 1. Forged signature -> 400, nothing changes.
        forged = await client.post(
            "/v1/provider/connect/webhook",
            content=payload,
            headers={"stripe-signature": "t=1,v1=deadbeef", "content-type": "application/json"},
        )
        assert forged.status_code == 400, forged.text

        # 2. Missing signature -> 400.
        unsigned = await client.post(
            "/v1/provider/connect/webhook",
            content=payload,
            headers={"content-type": "application/json"},
        )
        assert unsigned.status_code == 400, unsigned.text

        sm = get_sessionmaker()
        async with sm() as s:
            acct = (
                await s.execute(
                    select(ConnectAccount).where(
                        ConnectAccount.stripe_account_id == stripe_account_id
                    )
                )
            ).scalar_one()
            assert acct.payouts_enabled is False, "forged webhook must not enable payouts"

        # 3. Correctly signed -> applied.
        good = await client.post(
            "/v1/provider/connect/webhook",
            content=payload,
            headers={
                "stripe-signature": _stripe_sig(payload),
                "content-type": "application/json",
            },
        )
        assert good.status_code == 200, good.text
        assert good.json()["status"] == "account_updated"
        assert good.json()["payouts_enabled"] is True

        # 4. Signed with the WRONG secret -> 400.
        wrong = await client.post(
            "/v1/provider/connect/webhook",
            content=payload,
            headers={
                "stripe-signature": _stripe_sig(payload, "whsec_not_the_real_secret"),
                "content-type": "application/json",
            },
        )
        assert wrong.status_code == 400, wrong.text

    sm = get_sessionmaker()
    async with sm() as s:
        acct = (
            await s.execute(
                select(ConnectAccount).where(
                    ConnectAccount.stripe_account_id == stripe_account_id
                )
            )
        ).scalar_one()
    assert acct.payouts_enabled is True
    assert acct.status == "active"

    # The account row now satisfies the KYC gate, so a payout goes through.
    await _fund_earnings("c3_hook@example.com", "10.00")
    paid = await client.post(
        "/v1/provider/connect/payouts",
        json={"amount": "10.00", "idempotency_key": "payout-hook-0001"},
        headers=auth_headers(access),
    )
    assert paid.status_code == 201, paid.text
    await _assert_ledger_balanced()


async def test_failed_transfer_webhook_returns_money_to_provider(
    client: AsyncClient, fake_connect
):
    """transfer.failed reverses the payout back into provider earnings, once."""
    m = await create_listable_machine(client, "c3_fail@example.com", price="1.50")
    access = m["access"]
    provider_id = await _fund_earnings("c3_fail@example.com", "30.00")
    fake_connect.payouts_enabled = True
    await client.post("/v1/provider/connect/onboarding", json={}, headers=auth_headers(access))

    paid = await client.post(
        "/v1/provider/connect/payouts",
        json={"amount": "12.00", "idempotency_key": "payout-fail-0001"},
        headers=auth_headers(access),
    )
    assert paid.status_code == 201, paid.text
    payout_id = paid.json()["payout_id"]

    event = {
        "id": f"evt_{uuid.uuid4().hex[:16]}",
        "type": "transfer.failed",
        "data": {"object": {"id": "tr_x", "metadata": {"payout_id": payout_id}}},
    }
    payload = json.dumps(event).encode()

    import unittest.mock as _mock

    with _mock.patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        for _ in range(2):  # delivered twice: the reversal must happen once
            resp = await client.post(
                "/v1/provider/connect/webhook",
                content=payload,
                headers={
                    "stripe-signature": _stripe_sig(payload),
                    "content-type": "application/json",
                },
            )
            assert resp.status_code == 200, resp.text

    sm = get_sessionmaker()
    async with sm() as s:
        earnings = await ledger.get_provider_account(s, provider_id)
        assert earnings.balance_minor == 3000, "failed payout must be fully returned, once"
        payout = (
            await s.execute(
                select(ProviderPayout).where(ProviderPayout.id == uuid.UUID(payout_id))
            )
        ).scalar_one()
    assert payout.status == "cancelled"
    await _assert_ledger_balanced()


async def test_payout_requires_provider_and_account(client: AsyncClient, fake_connect):
    """A non-provider cannot reach the payout rail; a provider without an account 409s."""
    from tests.helpers import register_verify_login

    tokens = await register_verify_login(client, "c3_notprovider@example.com")
    r = await client.post(
        "/v1/provider/connect/payouts",
        json={"amount": "1.00", "idempotency_key": "payout-none-0001"},
        headers=auth_headers(tokens["access_token"]),
    )
    assert r.status_code == 403, r.text

    m = await create_listable_machine(client, "c3_noacct@example.com", price="1.50")
    await _fund_earnings("c3_noacct@example.com", "5.00")
    r2 = await client.post(
        "/v1/provider/connect/payouts",
        json={"amount": "1.00", "idempotency_key": "payout-none-0002"},
        headers=auth_headers(m["access"]),
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"] == "connect_account_missing"
    assert fake_connect.transfers == []
    await _assert_ledger_balanced()
