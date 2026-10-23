"""Vendor quota cards, renewal reminders and exactly-once alerting."""

from __future__ import annotations

import datetime as dt
import decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.enums import Role
from app.models.vendors import VendorAlert, VendorService
from app.services import vendors as vs
from tests.helpers import auth_headers, grant_role, login, register_verify_login


@pytest.fixture
async def admin(client: AsyncClient) -> dict:
    await register_verify_login(client, "vendor-admin@example.com")
    await grant_role("vendor-admin@example.com", Role.administrator)
    return await login(client, "vendor-admin@example.com")


@pytest.fixture
async def customer(client: AsyncClient) -> dict:
    return await register_verify_login(client, "vendor-cust@example.com")


async def _seed(**kw) -> VendorService:
    """Insert one service directly (the migration's seed is not present under
    Base.metadata.create_all, which is how the test schema is built)."""
    sm = get_sessionmaker()
    async with sm() as db:
        v = VendorService(
            key=kw.pop("key", "testvendor"),
            name=kw.pop("name", "Test Vendor"),
            category=kw.pop("category", "hosting"),
            purpose=kw.pop("purpose", "testing"),
            **kw,
        )
        db.add(v)
        await db.commit()
        await db.refresh(v)
        return v


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------
async def test_customer_cannot_see_vendor_services(client, customer):
    """Infrastructure spend is platform data, never customer-visible."""
    r = await client.get("/v1/admin/vendors", headers=auth_headers(customer["access_token"]))
    assert r.status_code == 403


async def test_anonymous_cannot_see_vendor_services(client):
    assert (await client.get("/v1/admin/vendors")).status_code in (401, 403)


async def test_admin_can_list(client, admin):
    await _seed(key="v-list")
    r = await client.get("/v1/admin/vendors", headers=auth_headers(admin["access_token"]))
    assert r.status_code == 200, r.text
    assert any(c["key"] == "v-list" for c in r.json())


# ---------------------------------------------------------------------------
# status is honest about what it does not know
# ---------------------------------------------------------------------------
async def test_a_service_with_no_usage_reading_is_unknown_not_ok(db_engine):
    """The whole point: an unmeasurable service must NOT render as healthy."""
    v = await _seed(key="v-unknown", quota_limit=decimal.Decimal("3000"),
                    quota_unit="emails/month", usage_source="unavailable")
    st = vs.evaluate(v)
    assert st.status == "unknown"
    assert st.usage_pct is None
    assert st.measurable is False
    assert "no usage api" in st.detail.lower() or "no usage reading" in st.detail.lower()


async def test_usage_billed_service_shows_no_percentage(db_engine):
    """Railway bills by compute-seconds; a '% of quota' bar would be invented."""
    v = await _seed(key="v-usage", quota_limit=None, billing_cycle="usage")
    st = vs.evaluate(v)
    assert st.usage_pct is None
    assert st.measurable is False
    assert "no fixed ceiling" in st.detail.lower()


async def test_measured_usage_is_labelled_as_an_estimate(db_engine):
    """A number we counted ourselves must never read as the vendor's invoice."""
    v = await _seed(key="v-measured", quota_limit=decimal.Decimal("100"),
                    current_usage=decimal.Decimal("10"), usage_source="measured")
    st = vs.evaluate(v)
    assert "estimate" in st.detail.lower()


@pytest.mark.parametrize(
    "usage,expected",
    [(10, "ok"), (55, "watch"), (85, "warning"), (100, "critical"), (140, "critical")],
)
async def test_status_thresholds(db_engine, usage, expected):
    v = await _seed(key=f"v-th-{usage}", quota_limit=decimal.Decimal("100"),
                    current_usage=decimal.Decimal(usage), usage_source="vendor_api")
    assert vs.evaluate(v).status == expected


# ---------------------------------------------------------------------------
# renewal reminders
# ---------------------------------------------------------------------------
async def test_renewal_inside_the_reminder_window_is_flagged(db_engine):
    soon = (utcnow() + dt.timedelta(days=3)).date()
    v = await _seed(key="v-renew", renewal_date=soon, reminder_days_before=7)
    st = vs.evaluate(v)
    assert st.renewal_due is True
    assert st.days_until_renewal == 3
    assert "renews in 3" in st.detail.lower()


async def test_renewal_outside_the_window_is_not_flagged(db_engine):
    far = (utcnow() + dt.timedelta(days=40)).date()
    v = await _seed(key="v-renew-far", renewal_date=far, reminder_days_before=7)
    st = vs.evaluate(v)
    assert st.renewal_due is False
    assert st.days_until_renewal == 40


async def test_a_passed_renewal_date_is_reported_not_hidden(db_engine):
    past = (utcnow() - dt.timedelta(days=2)).date()
    v = await _seed(key="v-renew-past", renewal_date=past)
    st = vs.evaluate(v)
    assert st.renewal_due is True
    assert st.days_until_renewal == -2
    assert "passed" in st.detail.lower()


async def test_no_renewal_date_means_no_reminder_and_no_guess(db_engine):
    v = await _seed(key="v-renew-none", renewal_date=None)
    st = vs.evaluate(v)
    assert st.days_until_renewal is None
    assert st.renewal_due is False


# ---------------------------------------------------------------------------
# alerts fire exactly once
# ---------------------------------------------------------------------------
async def test_usage_alert_fires_once_per_period(db_engine):
    await _seed(key="v-once", name="Once", quota_limit=decimal.Decimal("100"),
                current_usage=decimal.Decimal("85"), usage_source="vendor_api",
                quota_period="month")
    sm = get_sessionmaker()
    async with sm() as db:
        first = await vs.evaluate_and_alert(db)
        await db.commit()
        # 50 and 80 both crossed.
        assert {a.threshold_pct for a in first if a.alert_type == "usage_threshold"} == {50, 80}

        second = await vs.evaluate_and_alert(db)
        await db.commit()
        assert second == [], "the same crossing must not alert twice"


async def test_crossing_a_higher_threshold_later_still_alerts(db_engine):
    v = await _seed(key="v-climb", quota_limit=decimal.Decimal("100"),
                    current_usage=decimal.Decimal("55"), usage_source="vendor_api")
    sm = get_sessionmaker()
    async with sm() as db:
        await vs.evaluate_and_alert(db)
        await db.commit()
        row = await db.scalar(select(VendorService).where(VendorService.key == "v-climb"))
        row.current_usage = decimal.Decimal("100")
        await db.commit()
        more = await vs.evaluate_and_alert(db)
        await db.commit()
    assert {a.threshold_pct for a in more} == {80, 100}


async def test_renewal_alert_fires_once_then_again_for_the_next_date(db_engine):
    soon = (utcnow() + dt.timedelta(days=2)).date()
    await _seed(key="v-ren-once", name="Renewer", renewal_date=soon,
                reminder_days_before=7, plan_name="Hobby",
                cost_amount=decimal.Decimal("5.00"), billing_cycle="monthly")
    sm = get_sessionmaker()
    async with sm() as db:
        first = await vs.evaluate_and_alert(db)
        await db.commit()
        assert any(a.alert_type == "renewal_due" for a in first)
        assert await vs.evaluate_and_alert(db) == []
        await db.commit()

        # Rolling the subscription forward is a NEW reminder, not a duplicate.
        row = await db.scalar(select(VendorService).where(VendorService.key == "v-ren-once"))
        row.renewal_date = (utcnow() + dt.timedelta(days=1)).date()
        await db.commit()
        again = await vs.evaluate_and_alert(db)
        await db.commit()
    assert any(a.alert_type == "renewal_due" for a in again)


async def test_unmeasurable_service_never_fires_a_usage_alert(db_engine):
    """No reading means no alarm -- and no false all-clear either."""
    await _seed(key="v-silent", quota_limit=decimal.Decimal("3000"),
                usage_source="unavailable", current_usage=None)
    sm = get_sessionmaker()
    async with sm() as db:
        fired = await vs.evaluate_and_alert(db)
        await db.commit()
    assert [a for a in fired if a.alert_type == "usage_threshold"] == []


# ---------------------------------------------------------------------------
# admin API
# ---------------------------------------------------------------------------
async def test_admin_can_set_cost_and_renewal_then_sees_the_reminder(client, admin):
    v = await _seed(key="v-api", name="API Vendor")
    h = auth_headers(admin["access_token"])
    due = (utcnow() + dt.timedelta(days=2)).date()

    r = await client.patch(
        f"/v1/admin/vendors/{v.id}",
        json={
            "plan_name": "Pro", "cost_amount": "20.00",
            "billing_cycle": "monthly", "renewal_date": due.isoformat(),
            "reminder_days_before": 7,
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["cost_amount"] == "20.00"      # money is a 2dp string
    assert card["renewal_date"] == due.isoformat()
    assert card["renewal_due"] is True
    assert card["days_until_renewal"] == 2


async def test_summary_excludes_services_with_no_cost_set(client, admin):
    await _seed(key="v-sum-a", cost_amount=decimal.Decimal("10.00"), billing_cycle="monthly")
    await _seed(key="v-sum-b", cost_amount=None)
    r = await client.get("/v1/admin/vendors/summary", headers=auth_headers(admin["access_token"]))
    assert r.status_code == 200, r.text
    s = r.json()
    # The total is explicitly partial, and says how many it left out, rather
    # than implying it is the whole bill.
    assert s["services_without_cost"] >= 1
    assert decimal.Decimal(s["known_monthly_cost"]) >= decimal.Decimal("10.00")


async def test_annual_cost_is_normalised_to_monthly(client, admin):
    await _seed(key="v-annual", cost_amount=decimal.Decimal("120.00"), billing_cycle="annual")
    r = await client.get("/v1/admin/vendors/summary", headers=auth_headers(admin["access_token"]))
    assert decimal.Decimal(r.json()["known_monthly_cost"]) >= decimal.Decimal("10.00")


async def test_setting_usage_records_the_source(client, admin):
    v = await _seed(key="v-push", quota_limit=decimal.Decimal("100"))
    r = await client.post(
        f"/v1/admin/vendors/{v.id}/usage",
        json={"value": "90", "source": "manual"},
        headers=auth_headers(admin["access_token"]),
    )
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["usage_source"] == "manual"
    assert card["status"] == "warning"


async def test_alerts_are_listable_and_acknowledgeable(client, admin):
    v = await _seed(key="v-ack", quota_limit=decimal.Decimal("100"),
                    current_usage=decimal.Decimal("100"), usage_source="vendor_api")
    h = auth_headers(admin["access_token"])
    assert (await client.post("/v1/admin/vendors/evaluate", headers=h)).status_code == 200

    listed = await client.get("/v1/admin/vendors/alerts", headers=h)
    assert listed.status_code == 200
    mine = [a for a in listed.json() if a["vendor_id"] == str(v.id)]
    assert mine, "the 100% crossing should have produced an alert"

    ack = await client.post(f"/v1/admin/vendors/alerts/{mine[0]['id']}/acknowledge", headers=h)
    assert ack.status_code == 200
    assert ack.json()["acknowledged"] is True


async def test_acknowledging_does_not_re_arm_the_alert(client, admin):
    """Dismissing must not cause the same crossing to alert all over again."""
    v = await _seed(key="v-ack2", quota_limit=decimal.Decimal("100"),
                    current_usage=decimal.Decimal("100"), usage_source="vendor_api")
    h = auth_headers(admin["access_token"])
    await client.post("/v1/admin/vendors/evaluate", headers=h)
    listed = (await client.get("/v1/admin/vendors/alerts", headers=h)).json()
    mine = [a for a in listed if a["vendor_id"] == str(v.id)]
    await client.post(f"/v1/admin/vendors/alerts/{mine[0]['id']}/acknowledge", headers=h)

    again = await client.post("/v1/admin/vendors/evaluate", headers=h)
    assert [a for a in again.json() if a["vendor_id"] == str(v.id)] == []


async def test_update_on_unknown_vendor_is_404(client, admin):
    import uuid as _u
    r = await client.patch(
        f"/v1/admin/vendors/{_u.uuid4()}", json={"plan_name": "X"},
        headers=auth_headers(admin["access_token"]),
    )
    assert r.status_code == 404


async def test_customer_cannot_acknowledge_or_mutate(client, customer, admin):
    v = await _seed(key="v-guard")
    ch = auth_headers(customer["access_token"])
    assert (await client.patch(f"/v1/admin/vendors/{v.id}", json={"plan_name": "X"}, headers=ch)).status_code == 403
    assert (await client.post(f"/v1/admin/vendors/{v.id}/usage", json={"value": "1"}, headers=ch)).status_code == 403
    assert (await client.post("/v1/admin/vendors/evaluate", headers=ch)).status_code == 403
