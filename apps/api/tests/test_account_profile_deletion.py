"""Profile editing, password change and account deletion.

These were disabled placeholders in the console ("Profile editing is a placeholder",
"This demo button does not perform a real deletion"). Deletion anonymises the user
row rather than removing it, because jobs, invoices and the ledger reference it.
"""

from __future__ import annotations

import pytest

from app.models.enums import Role
from tests.helpers import auth_headers, grant_role, login, register_verify_login
from tests.test_phase4_jobs import seed_echo_model, submit_job
from tests.test_phase3_marketplace import create_listable_machine

PW = "Sup3rStrongPw!"
pytestmark = pytest.mark.asyncio


async def _user(client, email):
    tokens = await register_verify_login(client, email)
    return tokens["access_token"], auth_headers(tokens["access_token"])


# ---------------------------------------------------------------- profile
async def test_display_name_can_be_set_read_back_and_cleared(client):
    _, h = await _user(client, "name@x.io")
    r = await client.patch("/auth/me", json={"display_name": "  Ada Lovelace  "}, headers=h)
    assert r.status_code == 200 and r.json()["display_name"] == "Ada Lovelace"
    assert (await client.get("/auth/me", headers=h)).json()["display_name"] == "Ada Lovelace"
    cleared = await client.patch("/auth/me", json={"display_name": "   "}, headers=h)
    assert cleared.json()["display_name"] is None
    assert (await client.patch("/auth/me", json={"display_name": "x" * 121}, headers=h)).status_code == 422


# ---------------------------------------------------------- password change
async def test_change_password_needs_the_current_one_and_ends_other_sessions(client):
    first = await register_verify_login(client, "pw@x.io")
    h = auth_headers(first["access_token"])
    other = await client.post("/auth/login", json={"email": "pw@x.io", "password": PW})
    other_refresh = other.json()["refresh_token"]

    wrong = await client.post("/auth/change-password", json={"current_password": "nope-nope", "new_password": "BrandNew-Pass-9"}, headers=h)
    assert wrong.status_code == 400 and wrong.json()["detail"] == "current_password_incorrect"
    same = await client.post("/auth/change-password", json={"current_password": PW, "new_password": PW}, headers=h)
    assert same.status_code == 400
    short = await client.post("/auth/change-password", json={"current_password": PW, "new_password": "abc"}, headers=h)
    assert short.status_code == 422

    ok = await client.post("/auth/change-password", json={"current_password": PW, "new_password": "BrandNew-Pass-9"}, headers=h)
    assert ok.status_code == 200, ok.text
    assert (await client.post("/auth/login", json={"email": "pw@x.io", "password": PW})).status_code == 401
    assert (await client.post("/auth/login", json={"email": "pw@x.io", "password": "BrandNew-Pass-9"})).status_code == 200
    # Every session that existed before the change is dead.
    refreshed = await client.post("/auth/refresh", json={"refresh_token": other_refresh})
    assert refreshed.status_code == 401


# ------------------------------------------------------------ deletion rules
async def test_delete_requires_the_password(client):
    _, h = await _user(client, "del-pw@x.io")
    r = await client.post("/auth/delete-account", json={"password": "wrong-password", "acknowledge_forfeit": True}, headers=h)
    assert r.status_code == 400 and r.json()["detail"] == "password_incorrect"
    assert (await client.get("/auth/me", headers=h)).status_code == 200  # still there


async def test_credits_must_be_explicitly_forfeited(client):
    _, h = await _user(client, "del-credits@x.io")
    await client.post("/v1/billing/credits/purchase", json={"amount": "10"}, headers=h)
    r = await client.post("/auth/delete-account", json={"password": PW}, headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "credits_would_be_forfeited"
    assert r.headers["x-forfeit-amount"]
    assert (await client.get("/auth/me", headers=h)).status_code == 200


async def test_administrators_cannot_self_delete(client):
    await register_verify_login(client, "del-admin@x.io")
    await grant_role("del-admin@x.io", Role.administrator)
    tokens = await login(client, "del-admin@x.io")
    r = await client.post("/auth/delete-account", json={"password": PW, "acknowledge_forfeit": True}, headers=auth_headers(tokens["access_token"]))
    assert r.status_code == 409 and r.json()["detail"] == "administrator_cannot_self_delete"


async def test_active_jobs_block_deletion(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-del@x.io", hostname="del")
    token, h = await _user(client, "del-job@x.io")
    job = await submit_job(client, token)
    assert job.status_code == 201, job.text
    r = await client.post("/auth/delete-account", json={"password": PW, "acknowledge_forfeit": True}, headers=h)
    assert r.status_code == 409 and r.json()["detail"] in ("account_has_active_jobs", "account_has_held_credits")


# ----------------------------------------------------- what deletion does
async def test_deletion_anonymises_revokes_everything_and_frees_the_email(client):
    token, h = await _user(client, "gone@x.io")
    await client.patch("/auth/me", json={"display_name": "Gone Person"}, headers=h)
    project = (await client.post("/projects", json={"name": "p"}, headers=h)).json()
    key = (await client.post(f"/projects/{project['id']}/api-keys", json={"name": "k"}, headers=h)).json()["api_key"]
    refresh = (await client.post("/auth/login", json={"email": "gone@x.io", "password": PW})).json()["refresh_token"]
    assert (await client.get("/v1/jobs", headers=auth_headers(key))).status_code == 200

    r = await client.post("/auth/delete-account", json={"password": PW, "acknowledge_forfeit": True}, headers=h)
    assert r.status_code == 200, r.text

    # Gone: password login, the old token, the refresh token and the API key.
    assert (await client.post("/auth/login", json={"email": "gone@x.io", "password": PW})).status_code == 401
    assert (await client.get("/auth/me", headers=h)).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": refresh})).status_code == 401
    assert (await client.get("/v1/jobs", headers=auth_headers(key))).status_code == 401

    # The address is released: the same person can sign up again as a fresh account.
    again = await client.post("/auth/register", json={"email": "gone@x.io", "password": PW})
    assert again.status_code == 201, again.text

    # The anonymised row keeps nothing identifying.
    from sqlalchemy import select

    from app.db.session import get_sessionmaker
    from app.models.user import User

    async with get_sessionmaker()() as db:
        row = (await db.execute(select(User).where(User.email.like("deleted-%@deleted.invalid")))).scalars().all()
        assert len(row) == 1
        assert row[0].status.value == "deleted" and row[0].display_name is None and row[0].deleted_at is not None
        assert "gone" not in row[0].email


async def test_deleting_a_provider_deregisters_its_machines_and_keeps_the_ledger_balanced(client):
    from tests.test_provider_phase2 import become_provider, enroll_machine, mint_token

    prov = await become_provider(client, "prov-del@x.io")
    machine = await enroll_machine(client, (await mint_token(client, prov))["enrollment_token"], hostname="prov-del-box")
    h = auth_headers(prov)
    r = await client.post("/auth/delete-account", json={"password": PW, "acknowledge_forfeit": True}, headers=h)
    assert r.status_code == 200, r.text

    from app.db.session import get_sessionmaker
    from app.models.provider import ProviderMachine

    async with get_sessionmaker()() as db:
        m = await db.get(ProviderMachine, __import__("uuid").UUID(machine["machine_id"]))
        assert m.status == "deregistered" and m.availability == "offline" and m.signing_key == ""

    # Money records stay intact and balanced after the delete.
    await register_verify_login(client, "rec-admin@x.io")
    await grant_role("rec-admin@x.io", Role.administrator)
    admin = await login(client, "rec-admin@x.io")
    rec = (await client.get("/v1/admin/reconciliation", headers=auth_headers(admin["access_token"]))).json()
    assert rec["balanced"] is True


async def test_admin_cannot_reinstate_a_deleted_account(client):
    await register_verify_login(client, "adm2@x.io")
    await grant_role("adm2@x.io", Role.administrator)
    admin = await login(client, "adm2@x.io")
    tokens = await register_verify_login(client, "victim@x.io")
    uid = (await client.get("/auth/me", headers=auth_headers(tokens["access_token"]))).json()["id"]
    await client.post("/auth/delete-account", json={"password": PW, "acknowledge_forfeit": True}, headers=auth_headers(tokens["access_token"]))
    r = await client.post(f"/v1/admin/users/{uid}/reinstate", json={}, headers=auth_headers(admin["access_token"]))
    assert r.status_code == 409 and r.json()["detail"] == "account_deleted"
