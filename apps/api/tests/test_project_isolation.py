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


@pytest.mark.asyncio
async def test_project_description_is_stored_returned_and_editable(client):
    """The console collects a description; it used to be accepted and discarded."""
    u = await register_verify_login(client, "desc@example.com")
    h = auth_headers(u["access_token"])

    created = await client.post("/projects", json={"name": "p", "description": "  hello  "}, headers=h)
    assert created.status_code == 201, created.text
    assert created.json()["description"] == "hello"  # trimmed
    pid = created.json()["id"]

    assert (await client.get(f"/projects/{pid}", headers=h)).json()["description"] == "hello"
    listed = (await client.get("/projects", headers=h)).json()
    assert [p["description"] for p in listed] == ["hello"]

    # Omitting it on rename leaves it alone; an explicit null/blank clears it.
    assert (await client.patch(f"/projects/{pid}", json={"name": "p2"}, headers=h)).json()["description"] == "hello"
    cleared = await client.patch(f"/projects/{pid}", json={"name": "p2", "description": ""}, headers=h)
    assert cleared.json()["description"] is None

    too_long = await client.post("/projects", json={"name": "x", "description": "d" * 1001}, headers=h)
    assert too_long.status_code == 422

    no_desc = await client.post("/projects", json={"name": "plain"}, headers=h)
    assert no_desc.json()["description"] is None


@pytest.mark.asyncio
async def test_nul_character_database_error_is_a_422_not_a_500(client, monkeypatch):
    """Postgres rejects a NUL in text columns. That is bad input (422), not a crash;
    one handler covers every free-text field. Other DB errors must still be 500s."""
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    u = await register_verify_login(client, "nul@example.com")
    h = auth_headers(u["access_token"])

    class CharacterNotInRepertoireError(Exception):
        pass

    async def nul_flush(self, *a, **k):
        raise DBAPIError("INSERT", {}, CharacterNotInRepertoireError('invalid byte sequence 0x00'))

    monkeypatch.setattr(AsyncSession, "flush", nul_flush)
    r = await client.post("/projects", json={"name": "bad\u0000name"}, headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "invalid_characters"

    async def other_flush(self, *a, **k):
        raise DBAPIError("INSERT", {}, Exception("some other database failure"))

    monkeypatch.setattr(AsyncSession, "flush", other_flush)
    with pytest.raises(DBAPIError):  # unrelated DB errors are NOT swallowed
        await client.post("/projects", json={"name": "fine"}, headers=h)
