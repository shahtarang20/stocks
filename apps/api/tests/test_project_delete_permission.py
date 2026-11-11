"""Deleting an org project needs more than membership: the creator or an org admin/owner only."""

from __future__ import annotations

import pytest

from app.db.session import get_sessionmaker
from app.models.org import Organization, OrganizationMember
from tests.helpers import auth_headers, get_user_id, register_verify_login


async def _org_with(owner_email: str, others: dict[str, str]) -> "uuid.UUID":
    owner_id = await get_user_id(owner_email)
    async with get_sessionmaker()() as s:
        org = Organization(name="Delete-Perm-Org", owner_id=owner_id)
        s.add(org)
        await s.flush()
        s.add(OrganizationMember(org_id=org.id, user_id=owner_id, role="owner"))
        for email, role in others.items():
            s.add(OrganizationMember(org_id=org.id, user_id=await get_user_id(email), role=role))
        await s.commit()
        return org.id


@pytest.mark.asyncio
async def test_plain_member_cannot_delete_an_org_project_but_creator_and_admin_can(client):
    creator = await register_verify_login(client, "pd-creator@example.com")
    member = await register_verify_login(client, "pd-member@example.com")
    admin = await register_verify_login(client, "pd-admin@example.com")
    org_id = await _org_with(
        "pd-creator@example.com", {"pd-member@example.com": "member", "pd-admin@example.com": "admin"}
    )

    async def new_project(name):
        r = await client.post(
            "/projects", json={"name": name, "org_id": str(org_id)}, headers=auth_headers(creator["access_token"])
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    p1, p2, p3 = await new_project("p1"), await new_project("p2"), await new_project("p3")

    # A plain member can still see it, but not delete it.
    assert (await client.get(f"/projects/{p1}", headers=auth_headers(member["access_token"]))).status_code == 200
    denied = await client.delete(f"/projects/{p1}", headers=auth_headers(member["access_token"]))
    assert denied.status_code == 403, denied.text
    assert (await client.get(f"/projects/{p1}", headers=auth_headers(creator["access_token"]))).status_code == 200

    # The creator and an org admin can.
    assert (await client.delete(f"/projects/{p1}", headers=auth_headers(creator["access_token"]))).status_code == 204
    assert (await client.delete(f"/projects/{p2}", headers=auth_headers(admin["access_token"]))).status_code == 204
    # A non-member still gets 404, not 403 (existence is not revealed).
    stranger = await register_verify_login(client, "pd-stranger@example.com")
    assert (await client.delete(f"/projects/{p3}", headers=auth_headers(stranger["access_token"]))).status_code == 404
