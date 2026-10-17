"""Phase 3 — marketplace search, quotes, atomic reservations tests."""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import hashlib
import hmac
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.main import app
from app.models.marketplace import PricingRule, Quote, ResourceReservation
from app.services.pricing import compute_breakdown, get_active_fee_rate
from tests.helpers import auth_headers, register_verify_login

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _capability_report(workloads=None) -> dict:
    return {
        "verification_status": "detected",
        "cpu_model": "AMD EPYC 7542",
        "cpu_cores": 32,
        "ram_mb": 262144,
        "disk_gb": 2000,
        "gpus": [{"model": "NVIDIA A100", "vram_mb": 81920, "cuda_version": "12.2"}],
        "supported_workloads": workloads or ["text-generation", "batch-inference"],
    }


def _heartbeat_headers(machine_id: str, machine_secret: str, body: dict) -> tuple[dict, bytes]:
    raw = json.dumps(body).encode("utf-8")
    ts = str(int(utcnow().timestamp()))
    signing_key = machine_secret.removeprefix("msk_")
    body_hash = hashlib.sha256(raw).hexdigest()
    canonical = f"{machine_id}\n{ts}\n{body_hash}"
    sig = hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-ComputeCoin-Machine": machine_id,
        "X-ComputeCoin-Timestamp": ts,
        "X-ComputeCoin-Signature": sig,
        "Content-Type": "application/json",
    }
    return headers, raw


async def _become_provider(client: AsyncClient, email: str) -> str:
    tokens = await register_verify_login(client, email)
    access = tokens["access_token"]
    resp = await client.post(
        "/provider/register",
        json={"display_name": "Acme GPUs", "contact_email": email},
        headers=auth_headers(access),
    )
    assert resp.status_code == 200, resp.text
    return access


async def _send_heartbeat(client: AsyncClient, machine_id: str, secret: str, availability="online"):
    body = {
        "availability": availability,
        "capability_summary": {"gpu_count": 1, "vram_mb": 81920},
        "health": {"uptime_s": 10},
    }
    headers, raw = _heartbeat_headers(machine_id, secret, body)
    resp = await client.post("/v1/agents/heartbeat", content=raw, headers=headers)
    assert resp.status_code == 200, resp.text


async def create_listable_machine(
    client: AsyncClient,
    email: str,
    *,
    price: str = "1.50",
    max_concurrent_jobs: int = 1,
    region: str | None = "us-east",
    workloads=None,
    gpus=None,
    online: bool = True,
    priced: bool = True,
    hostname: str = "gpu-box",
) -> dict:
    """Create a provider machine and (optionally) bring it online + priced."""
    access = await _become_provider(client, email)
    tok = await client.post(
        "/provider/enrollment-tokens", json={"label": "l"}, headers=auth_headers(access)
    )
    raw = tok.json()["enrollment_token"]
    report = _capability_report(workloads)
    if gpus is not None:
        report["gpus"] = gpus
    enroll = await client.post(
        "/v1/agents/enroll",
        json={
            "enrollment_token": raw,
            "agent_version": "0.1.0",
            "hostname": hostname,
            "capability_report": report,
        },
    )
    assert enroll.status_code == 201, enroll.text
    machine_id = enroll.json()["machine_id"]
    secret = enroll.json()["machine_secret"]

    patch_body: dict = {"max_concurrent_jobs": max_concurrent_jobs, "region": region}
    if priced:
        patch_body["asking_price_per_hour"] = price
    if workloads is not None:
        patch_body["supported_workloads"] = workloads
    r = await client.patch(
        f"/provider/machines/{machine_id}", json=patch_body, headers=auth_headers(access)
    )
    assert r.status_code == 200, r.text

    if online:
        await _send_heartbeat(client, machine_id, secret)
    return {"machine_id": machine_id, "secret": secret, "access": access}


async def _customer(client: AsyncClient, email: str) -> str:
    tokens = await register_verify_login(client, email)
    return tokens["access_token"]


# ===========================================================================
# 1. pricing math
# ===========================================================================
async def test_pricing_math_exact_example():
    bd = compute_breakdown(
        price_per_hour=decimal.Decimal("1.50"),
        estimated_minutes=60,
        gpu_count=1,
        platform_fee_rate=decimal.Decimal("0.10"),
    )
    assert bd.estimated_hours == decimal.Decimal("1.00")
    assert bd.gpu_cost == decimal.Decimal("1.50")
    assert bd.platform_fee == decimal.Decimal("0.15")
    assert bd.total == decimal.Decimal("1.65")


async def test_pricing_math_rounding_half_up():
    # gpu_cost 1.50 @ 5% fee = 0.075 -> rounds HALF_UP to 0.08.
    bd = compute_breakdown(
        price_per_hour=decimal.Decimal("1.50"),
        estimated_minutes=60,
        gpu_count=1,
        platform_fee_rate=decimal.Decimal("0.05"),
    )
    assert bd.gpu_cost == decimal.Decimal("1.50")
    assert bd.platform_fee == decimal.Decimal("0.08")
    assert bd.total == decimal.Decimal("1.58")

    # Fractional hours: 50 min @ 1.00/hr -> 0.8333.. -> 0.83 gpu_cost.
    bd2 = compute_breakdown(
        price_per_hour=decimal.Decimal("1.00"),
        estimated_minutes=50,
        gpu_count=1,
        platform_fee_rate=decimal.Decimal("0.10"),
    )
    assert bd2.gpu_cost == decimal.Decimal("0.83")
    assert bd2.platform_fee == decimal.Decimal("0.08")
    assert bd2.total == decimal.Decimal("0.91")


async def test_get_active_fee_rate_reads_pricing_rule(db_engine):
    sm = get_sessionmaker()
    async with sm() as s:
        s.add(PricingRule(name="default", platform_fee_rate=decimal.Decimal("0.15"), active=True))
        await s.commit()
    async with sm() as s:
        rate = await get_active_fee_rate(s)
    assert rate == decimal.Decimal("0.15")


# ===========================================================================
# 2. marketplace search
# ===========================================================================
async def test_search_lists_only_listable_machines(client: AsyncClient):
    await create_listable_machine(client, "p-online@x.com", hostname="on", region="us-east")
    # Offline machine (no heartbeat) must be excluded.
    await create_listable_machine(
        client, "p-offline@x.com", hostname="off", online=False, region="us-west"
    )
    # Unpriced machine must be excluded.
    await create_listable_machine(
        client, "p-unpriced@x.com", hostname="np", priced=False, region="eu"
    )

    resp = await client.get("/v1/marketplace/machines")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["price_per_hour"] == "1.50"
    assert item["currency"] == "USDC_CREDITS"
    assert item["available_slots"] == 1
    assert item["max_concurrent_jobs"] == 1
    assert item["gpu_model"] == "NVIDIA A100"
    assert item["reliability"]["success_rate"] is None
    assert item["reliability"]["completed_jobs"] == 0
    assert "text-generation" in item["supported_workloads"]


async def test_search_filters(client: AsyncClient):
    await create_listable_machine(
        client, "f1@x.com", hostname="a", price="1.00", region="us-east",
        workloads=["text-generation"],
        gpus=[{"model": "NVIDIA A100", "vram_mb": 81920, "cuda_version": "12.2"}],
    )
    await create_listable_machine(
        client, "f2@x.com", hostname="b", price="5.00", region="eu-west",
        workloads=["batch-inference"],
        gpus=[{"model": "NVIDIA T4", "vram_mb": 16384, "cuda_version": "12.2"}],
    )

    # max_price filter.
    r = await client.get("/v1/marketplace/machines", params={"max_price_per_hour": "2.00"})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["price_per_hour"] == "1.00"

    # min_vram filter.
    r = await client.get("/v1/marketplace/machines", params={"min_vram_mb": 20000})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["gpu_model"] == "NVIDIA A100"

    # region filter.
    r = await client.get("/v1/marketplace/machines", params={"region": "eu-west"})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["price_per_hour"] == "5.00"

    # workload filter.
    r = await client.get("/v1/marketplace/machines", params={"workload": "batch-inference"})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["price_per_hour"] == "5.00"

    # gpu_model filter.
    r = await client.get("/v1/marketplace/machines", params={"gpu_model": "T4"})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["gpu_model"] == "NVIDIA T4"


async def test_search_pagination(client: AsyncClient):
    for i in range(3):
        await create_listable_machine(client, f"pg{i}@x.com", hostname=f"m{i}", region="us")
    r = await client.get("/v1/marketplace/machines", params={"page": 1, "page_size": 2})
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["page"] == 1 and body["page_size"] == 2
    r2 = await client.get("/v1/marketplace/machines", params={"page": 2, "page_size": 2})
    assert len(r2.json()["items"]) == 1


async def test_get_single_machine_public(client: AsyncClient):
    m = await create_listable_machine(client, "single@x.com", hostname="s")
    r = await client.get(f"/v1/marketplace/machines/{m['machine_id']}")
    assert r.status_code == 200
    assert r.json()["machine_id"] == m["machine_id"]
    # Unknown / not listable -> 404.
    r404 = await client.get(f"/v1/marketplace/machines/{uuid.uuid4()}")
    assert r404.status_code == 404


# ===========================================================================
# 3. quotes
# ===========================================================================
async def test_quote_happy_path_itemization(client: AsyncClient):
    m = await create_listable_machine(client, "qp@x.com", hostname="q")
    cust = await _customer(client, "cust-q@x.com")
    r = await client.post(
        "/v1/quotes",
        json={
            "machine_id": m["machine_id"],
            "workload_type": "text-generation",
            "estimated_minutes": 60,
            "gpu_count": 1,
        },
        headers=auth_headers(cust),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "active"
    bd = body["breakdown"]
    assert bd["price_per_hour"] == "1.50"
    assert bd["estimated_hours"] == "1.00"
    assert bd["gpu_cost"] == "1.50"
    assert bd["platform_fee_rate"] == "0.10"
    assert bd["platform_fee"] == "0.15"
    assert bd["total"] == "1.65"
    assert body["currency"] == "USDC_CREDITS"


async def test_quote_workload_not_supported_422(client: AsyncClient):
    m = await create_listable_machine(
        client, "qw@x.com", hostname="qw", workloads=["text-generation"]
    )
    cust = await _customer(client, "cust-w@x.com")
    r = await client.post(
        "/v1/quotes",
        json={
            "machine_id": m["machine_id"],
            "workload_type": "image-generation",
            "estimated_minutes": 60,
            "gpu_count": 1,
        },
        headers=auth_headers(cust),
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "workload_not_supported"


async def test_quote_gpu_count_too_high_422(client: AsyncClient):
    m = await create_listable_machine(client, "qg@x.com", hostname="qg")  # 1 gpu
    cust = await _customer(client, "cust-g@x.com")
    r = await client.post(
        "/v1/quotes",
        json={
            "machine_id": m["machine_id"],
            "workload_type": "text-generation",
            "estimated_minutes": 60,
            "gpu_count": 4,
        },
        headers=auth_headers(cust),
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "gpu_count_exceeds_machine"


async def test_quote_budget_exceeded_409(client: AsyncClient):
    m = await create_listable_machine(client, "qb@x.com", hostname="qb")
    cust = await _customer(client, "cust-b@x.com")
    r = await client.post(
        "/v1/quotes",
        json={
            "machine_id": m["machine_id"],
            "workload_type": "text-generation",
            "estimated_minutes": 60,
            "gpu_count": 1,
            "max_authorized_spend": "1.00",  # total would be 1.65
        },
        headers=auth_headers(cust),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "budget_exceeded"


async def test_quote_on_offline_machine_404(client: AsyncClient):
    m = await create_listable_machine(client, "qo@x.com", hostname="qo", online=False)
    cust = await _customer(client, "cust-o@x.com")
    r = await client.post(
        "/v1/quotes",
        json={
            "machine_id": m["machine_id"],
            "workload_type": "text-generation",
            "estimated_minutes": 60,
            "gpu_count": 1,
        },
        headers=auth_headers(cust),
    )
    assert r.status_code == 404, r.text


async def test_reserve_on_expired_quote_409(client: AsyncClient):
    m = await create_listable_machine(client, "qe@x.com", hostname="qe")
    cust = await _customer(client, "cust-e@x.com")
    r = await client.post(
        "/v1/quotes",
        json={
            "machine_id": m["machine_id"],
            "workload_type": "text-generation",
            "estimated_minutes": 60,
            "gpu_count": 1,
        },
        headers=auth_headers(cust),
    )
    quote_id = r.json()["quote_id"]
    # Force expiry in DB.
    sm = get_sessionmaker()
    async with sm() as s:
        q = (await s.execute(select(Quote).where(Quote.id == uuid.UUID(quote_id)))).scalar_one()
        q.expires_at = utcnow() - dt.timedelta(seconds=5)
        await s.commit()

    rr = await client.post(f"/v1/quotes/{quote_id}/reserve", json={}, headers=auth_headers(cust))
    assert rr.status_code == 409, rr.text
    assert rr.json()["detail"] == "quote_expired"


# ===========================================================================
# 4. reservations
# ===========================================================================
async def _make_quote(client, machine_id, cust, gpu_count=1, minutes=60):
    r = await client.post(
        "/v1/quotes",
        json={
            "machine_id": machine_id,
            "workload_type": "text-generation",
            "estimated_minutes": minutes,
            "gpu_count": gpu_count,
        },
        headers=auth_headers(cust),
    )
    assert r.status_code == 201, r.text
    return r.json()["quote_id"]


async def test_reserve_happy_path_and_slot_decrement(client: AsyncClient):
    m = await create_listable_machine(client, "rh@x.com", hostname="rh", max_concurrent_jobs=1)
    cust = await _customer(client, "cust-rh@x.com")
    q = await _make_quote(client, m["machine_id"], cust)

    rr = await client.post(f"/v1/quotes/{q}/reserve", json={}, headers=auth_headers(cust))
    assert rr.status_code == 201, rr.text
    assert rr.json()["status"] == "active"
    assert rr.json()["slots"] == 1

    # Quote is now reserved.
    quote = await client.get(f"/v1/quotes/{q}", headers=auth_headers(cust))
    assert quote.json()["status"] == "reserved"

    # Search shows available_slots decremented to 0.
    search = await client.get(f"/v1/marketplace/machines/{m['machine_id']}")
    assert search.json()["available_slots"] == 0


async def test_reserve_same_quote_twice_409(client: AsyncClient):
    m = await create_listable_machine(client, "rt@x.com", hostname="rt", max_concurrent_jobs=2)
    cust = await _customer(client, "cust-rt@x.com")
    q = await _make_quote(client, m["machine_id"], cust)
    first = await client.post(f"/v1/quotes/{q}/reserve", json={}, headers=auth_headers(cust))
    assert first.status_code == 201
    second = await client.post(f"/v1/quotes/{q}/reserve", json={}, headers=auth_headers(cust))
    assert second.status_code == 409
    assert second.json()["detail"] == "quote_already_reserved"


async def test_reserve_beyond_capacity_409(client: AsyncClient):
    m = await create_listable_machine(client, "rc@x.com", hostname="rc", max_concurrent_jobs=1)
    cust = await _customer(client, "cust-rc@x.com")
    q1 = await _make_quote(client, m["machine_id"], cust)
    q2 = await _make_quote(client, m["machine_id"], cust)
    r1 = await client.post(f"/v1/quotes/{q1}/reserve", json={}, headers=auth_headers(cust))
    assert r1.status_code == 201
    r2 = await client.post(f"/v1/quotes/{q2}/reserve", json={}, headers=auth_headers(cust))
    assert r2.status_code == 409
    assert r2.json()["detail"] == "no_capacity"


async def test_release_frees_slot(client: AsyncClient):
    m = await create_listable_machine(client, "rf@x.com", hostname="rf", max_concurrent_jobs=1)
    cust = await _customer(client, "cust-rf@x.com")
    q1 = await _make_quote(client, m["machine_id"], cust)
    r1 = await client.post(f"/v1/quotes/{q1}/reserve", json={}, headers=auth_headers(cust))
    reservation_id = r1.json()["reservation_id"]

    # Capacity full now; a new quote cannot reserve.
    q2 = await _make_quote(client, m["machine_id"], cust)
    assert (
        await client.post(f"/v1/quotes/{q2}/reserve", json={}, headers=auth_headers(cust))
    ).status_code == 409

    # Release frees the slot.
    rel = await client.post(
        f"/v1/reservations/{reservation_id}/release", json={}, headers=auth_headers(cust)
    )
    assert rel.status_code == 200, rel.text
    assert rel.json()["status"] == "released"

    # Idempotent release.
    rel2 = await client.post(
        f"/v1/reservations/{reservation_id}/release", json={}, headers=auth_headers(cust)
    )
    assert rel2.status_code == 200

    # Search shows slot back.
    search = await client.get(f"/v1/marketplace/machines/{m['machine_id']}")
    assert search.json()["available_slots"] == 1

    # A new reserve now succeeds.
    r2 = await client.post(f"/v1/quotes/{q2}/reserve", json={}, headers=auth_headers(cust))
    assert r2.status_code == 201, r2.text


# ===========================================================================
# 5. CONCURRENCY — last-slot race
# ===========================================================================
async def test_reserve_concurrency_exactly_one_wins(client: AsyncClient):
    m = await create_listable_machine(client, "cc@x.com", hostname="cc", max_concurrent_jobs=1)
    cust = await _customer(client, "cust-cc@x.com")
    q1 = await _make_quote(client, m["machine_id"], cust)
    q2 = await _make_quote(client, m["machine_id"], cust)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c1, AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c2:
        results = await asyncio.gather(
            c1.post(f"/v1/quotes/{q1}/reserve", json={}, headers=auth_headers(cust)),
            c2.post(f"/v1/quotes/{q2}/reserve", json={}, headers=auth_headers(cust)),
        )
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409], [r.status_code for r in results]
    losers = [r for r in results if r.status_code == 409]
    assert losers[0].json()["detail"] == "no_capacity"

    # DB invariant: active reservation slots for the machine never exceed 1.
    sm = get_sessionmaker()
    async with sm() as s:
        rows = (
            await s.execute(
                select(ResourceReservation).where(
                    ResourceReservation.machine_id == uuid.UUID(m["machine_id"]),
                    ResourceReservation.status == "active",
                )
            )
        ).scalars().all()
    assert sum(r.slots for r in rows) == 1


# ===========================================================================
# 6. isolation
# ===========================================================================
async def test_customer_isolation(client: AsyncClient):
    m = await create_listable_machine(client, "iso@x.com", hostname="iso", max_concurrent_jobs=2)
    cust_a = await _customer(client, "cust-a@x.com")
    cust_b = await _customer(client, "cust-b2@x.com")

    q = await _make_quote(client, m["machine_id"], cust_a)
    # B cannot read A's quote.
    assert (await client.get(f"/v1/quotes/{q}", headers=auth_headers(cust_b))).status_code == 404
    # B cannot reserve A's quote.
    assert (
        await client.post(f"/v1/quotes/{q}/reserve", json={}, headers=auth_headers(cust_b))
    ).status_code == 404

    # A reserves; B cannot release A's reservation.
    r = await client.post(f"/v1/quotes/{q}/reserve", json={}, headers=auth_headers(cust_a))
    reservation_id = r.json()["reservation_id"]
    assert (
        await client.post(
            f"/v1/reservations/{reservation_id}/release", json={}, headers=auth_headers(cust_b)
        )
    ).status_code == 404

    # B's reservation list is empty; A's is not.
    assert (await client.get("/v1/reservations", headers=auth_headers(cust_b))).json() == []
    assert len((await client.get("/v1/reservations", headers=auth_headers(cust_a))).json()) == 1
