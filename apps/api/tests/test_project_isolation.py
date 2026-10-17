"""Account isolation: user A cannot see/modify user B's projects or keys."""

from __future__ import annotations

import pytest

from tests.helpers import auth_headers, register_verify_login


@pytest.mark.asyncio
async def test_user_cannot_access_other_users_project(client):
    a = await register_verify_login(client, "ann@example.com")
    b = await register_verify_login(client, "ben@example.com")

    # B creates a project.
    created = await client.post(
        "/projects", json={"name": "ben-secret"}, headers=auth_headers(b["access_token"])
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    # A cannot GET it -> 404 (existence hidden).
    get_resp = await client.get(
        f"/projects/{project_id}", headers=auth_headers(a["access_token"])
    )
    assert get_resp.status_code == 404

    # A cannot DELETE it -> 404.
    del_resp = await client.delete(
        f"/projects/{project_id}", headers=auth_headers(a["access_token"])
    )
    assert del_resp.status_code == 404

    # A cannot list B's api keys -> 404.
    keys_resp = await client.get(
        f"/projects/{project_id}/api-keys", headers=auth_headers(a["access_token"])
    )
    assert keys_resp.status_code == 404

    # B can still access their own project.
    owner_get = await client.get(
        f"/projects/{project_id}", headers=auth_headers(b["access_token"])
    )
    assert owner_get.status_code == 200


@pytest.mark.asyncio
async def test_list_only_returns_own_projects(client):
    a = await register_verify_login(client, "cora@example.com")
    b = await register_verify_login(client, "dan@example.com")

    await client.post(
        "/projects", json={"name": "a-proj"}, headers=auth_headers(a["access_token"])
    )
    await client.post(
        "/projects", json={"name": "b-proj"}, headers=auth_headers(b["access_token"])
    )

    resp = await client.get("/projects", headers=auth_headers(a["access_token"]))
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert names == {"a-proj"}
