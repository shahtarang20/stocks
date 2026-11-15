"""An administrator can mark a user's email as confirmed by hand (while email cannot yet reach that person).

It skips the proof that the person owns the mailbox, so it needs a written reason, is audited, and is for administrators only.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.enums import Role, UserStatus
from app.models.system import AuditLog
from app.models.user import User
from tests.helpers import auth_headers, get_user_id, grant_role, login, register_user, register_verify_login

pytestmark = pytest.mark.asyncio

REASON = "Known host, email delivery not set up yet"


async def _admin(client, email="vf-admin@example.com"):
    await register_verify_login(client, email)
    await grant_role(email, Role.administrator)
    return (await login(client, email))["access_token"]


async def _unverified(client, email):
    """A registered user who never confirmed their email (as when the confirmation email cannot be delivered)."""
    await register_user(client, email)
    return (await login(client, email))["access_token"]


async def _user(email) -> User:
    async with get_sessionmaker()() as s:
        return (await s.execute(select(User).where(User.email == email))).scalar_one()


async def _audit(action) -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        return list((await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all())


SPEND = {"scope": "customer", "period": "monthly", "limit_minor": 50000, "alert_thresholds": [50, 80]}


async def test_an_unverified_account_is_blocked_from_verified_only_actions_until_an_admin_confirms_it(client):
    admin = await _admin(client)
    token = await _unverified(client, "vf-friend@example.com")
    blocked = await client.post("/spend-limits", json=SPEND, headers=auth_headers(token))
    assert blocked.status_code == 403 and blocked.json()["detail"] == "email_not_verified"

    uid = await get_user_id("vf-friend@example.com")
    done = await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(admin))
    assert done.status_code == 200, done.text
    assert done.json()["email"] == "vf-friend@example.com" and "marked as confirmed" in done.json()["message"]
    assert (await _user("vf-friend@example.com")).email_verified is True

    # the SAME login token works at once: the flag is read live, the friend does not have to log in again
    allowed = await client.post("/spend-limits", json=SPEND, headers=auth_headers(token))
    assert allowed.status_code == 201, allowed.text


async def test_the_users_list_shows_the_new_state(client):
    admin = await _admin(client)
    await _unverified(client, "vf-list@example.com")
    uid = await get_user_id("vf-list@example.com")
    before = {u["email"]: u for u in (await client.get("/v1/admin/users", headers=auth_headers(admin))).json()}
    assert before["vf-list@example.com"]["email_verified"] is False
    await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(admin))
    after = {u["email"]: u for u in (await client.get("/v1/admin/users", headers=auth_headers(admin))).json()}
    assert after["vf-list@example.com"]["email_verified"] is True


async def test_it_is_audited_with_who_what_and_why(client):
    admin = await _admin(client)
    await _unverified(client, "vf-audit@example.com")
    uid = await get_user_id("vf-audit@example.com")
    admin_id = await get_user_id("vf-admin@example.com")
    await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": f"  {REASON}  "}, headers=auth_headers(admin))
    rows = await _audit("admin.user.verify_email")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == admin_id and row.resource_id == str(uid) and row.resource_type == "user"
    assert row.outcome == "success" and row.reason == REASON  # the reason is kept, trimmed


async def test_asking_again_changes_nothing_and_adds_no_audit_row(client):
    admin = await _admin(client)
    await _unverified(client, "vf-twice@example.com")
    uid = await get_user_id("vf-twice@example.com")
    first = await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(admin))
    second = await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(admin))
    assert first.status_code == second.status_code == 200
    assert "already confirmed" in second.json()["message"]
    assert len(await _audit("admin.user.verify_email")) == 1


async def test_an_already_verified_user_is_left_alone(client):
    admin = await _admin(client)
    await register_verify_login(client, "vf-fine@example.com")
    uid = await get_user_id("vf-fine@example.com")
    res = await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(admin))
    assert res.status_code == 200 and "already confirmed" in res.json()["message"]
    assert await _audit("admin.user.verify_email") == []


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "    "}, {"reason": "no"}, {"reason": "x" * 301}, {"reason": 5}, {"why": REASON}])
async def test_a_written_reason_is_required(client, body):
    admin = await _admin(client)
    await _unverified(client, "vf-reason@example.com")
    uid = await get_user_id("vf-reason@example.com")
    res = await client.post(f"/v1/admin/users/{uid}/verify-email", json=body, headers=auth_headers(admin))
    assert res.status_code == 422
    assert (await _user("vf-reason@example.com")).email_verified is False
    assert await _audit("admin.user.verify_email") == []


async def test_only_administrators_can_do_it(client):
    await _admin(client)
    friend = await _unverified(client, "vf-victim@example.com")
    uid = await get_user_id("vf-victim@example.com")
    # the person themselves cannot confirm their own email this way
    assert (await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(friend))).status_code == 403
    # nor an ordinary verified user, nor a host
    other = await register_verify_login(client, "vf-other@example.com")
    assert (await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(other["access_token"]))).status_code == 403
    await grant_role("vf-other@example.com", Role.provider)
    host = await login(client, "vf-other@example.com")
    assert (await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(host["access_token"]))).status_code == 403
    # and not without logging in
    assert (await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON})).status_code in (401, 403)
    assert (await _user("vf-victim@example.com")).email_verified is False
    assert await _audit("admin.user.verify_email") == []


async def test_an_api_key_never_carries_this_power(client):
    """Keys sit in config files; the platform already refuses operator routes to them. This one is no exception."""
    admin = await _admin(client)
    await _unverified(client, "vf-key@example.com")
    uid = await get_user_id("vf-key@example.com")
    created = await client.post("/projects", json={"name": "vf-proj"}, headers=auth_headers(admin))
    assert created.status_code == 201, created.text
    key = await client.post(f"/projects/{created.json()['id']}/api-keys", json={"name": "k"}, headers=auth_headers(admin))
    assert key.status_code == 201, key.text
    raw = key.json()["api_key"]
    res = await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers={"Authorization": f"Bearer {raw}"})
    assert res.status_code == 403
    assert (await _user("vf-key@example.com")).email_verified is False


async def test_unknown_and_deleted_accounts_are_refused(client):
    admin = await _admin(client)
    import uuid

    missing = await client.post(f"/v1/admin/users/{uuid.uuid4()}/verify-email", json={"reason": REASON}, headers=auth_headers(admin))
    assert missing.status_code == 404 and missing.json()["detail"] == "user_not_found"
    await _unverified(client, "vf-gone@example.com")
    async with get_sessionmaker()() as s:
        user = (await s.execute(select(User).where(User.email == "vf-gone@example.com"))).scalar_one()
        user.status = UserStatus.deleted
        await s.commit()
        uid = user.id
    gone = await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(admin))
    assert gone.status_code == 409 and gone.json()["detail"] == "account_deleted"
    assert (await _user("vf-gone@example.com")).email_verified is False


async def test_a_suspended_account_can_be_confirmed_but_stays_suspended(client):
    admin = await _admin(client)
    await _unverified(client, "vf-susp@example.com")
    uid = await get_user_id("vf-susp@example.com")
    assert (await client.post(f"/v1/admin/users/{uid}/suspend", headers=auth_headers(admin))).status_code == 200
    res = await client.post(f"/v1/admin/users/{uid}/verify-email", json={"reason": REASON}, headers=auth_headers(admin))
    assert res.status_code == 200 and res.json()["status"] == "suspended"  # confirming an email does not lift a suspension
    user = await _user("vf-susp@example.com")
    assert user.email_verified is True and user.status == UserStatus.suspended


async def test_the_route_exists_in_the_app():
    from app.main import app

    assert "/v1/admin/users/{user_id}/verify-email" in {getattr(r, "path", None) for r in app.routes}
