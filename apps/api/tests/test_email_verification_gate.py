"""Hardening: an unverified-email account cannot spend credits or run compute
(closes the throwaway-account abuse vector found in the live red-team pass).
Read access and verification still work; after verifying, the account is allowed.
"""
import pytest

PW = "Str0ngPass-123!"
JOB = {
    "workload_type": "text-generation", "model": "echo-sim-v1",
    "input": {"prompt": "x"}, "gpu_count": 1, "estimated_minutes": 5,
    "max_cost": "5.00", "max_runtime_seconds": 60,
}


@pytest.mark.asyncio
async def test_unverified_user_blocked_from_spend_and_compute(client):
    reg = await client.post("/auth/register", json={"email": "unv-gate@x.io", "password": PW})
    verify_token = reg.json()["email_verification_token"]
    login = await client.post("/auth/login", json={"email": "unv-gate@x.io", "password": PW})
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Sensitive money/compute actions are blocked for an unverified account.
    assert (await client.post("/v1/billing/credits/purchase", json={"amount": "10.00"}, headers=h)).status_code == 403
    assert (await client.post("/v1/jobs", json=JOB, headers=h)).status_code == 403
    chat = await client.post(
        "/v1/chat/completions",
        json={"model": "echo-sim-v1", "messages": [{"role": "user", "content": "hi"}]},
        headers=h,
    )
    assert chat.status_code == 403

    # Read access still works (not gated).
    assert (await client.get("/v1/jobs", headers=h)).status_code == 200

    # After verifying the email, the SAME token is now allowed (email_verified is
    # read fresh from the DB on each request).
    await client.post("/auth/verify-email", json={"token": verify_token})
    assert (await client.post("/v1/billing/credits/purchase", json={"amount": "10.00"}, headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_resend_verification_issues_a_working_fresh_link(client, monkeypatch):
    sent: list[str] = []

    async def fake_send(to_email, raw_token):
        sent.append(raw_token)

    monkeypatch.setattr("app.api.routers.auth.send_verification_email", fake_send)

    await client.post("/auth/register", json={"email": "resend@x.io", "password": PW})
    login = await client.post("/auth/login", json={"email": "resend@x.io", "password": PW})
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Needs a session: anonymous callers cannot trigger or probe it.
    client.cookies.clear()  # drop the login cookies; the Bearer header carries auth below
    assert (await client.post("/auth/resend-verification")).status_code == 401

    sent.clear()
    r = await client.post("/auth/resend-verification", headers=h)
    assert r.status_code == 200 and sent, r.text
    assert (await client.post("/auth/verify-email", json={"token": sent[0]})).status_code == 200
    assert (await client.post("/v1/billing/credits/purchase", json={"amount": "10.00"}, headers=h)).status_code == 200

    # Already verified: no second email.
    sent.clear()
    again = await client.post("/auth/resend-verification", headers=h)
    assert again.json()["message"] == "Email already verified" and not sent


@pytest.mark.asyncio
async def test_resend_verification_is_rate_limited(client, monkeypatch):
    async def fake_send(to_email, raw_token):
        return None

    monkeypatch.setattr("app.api.routers.auth.send_verification_email", fake_send)
    await client.post("/auth/register", json={"email": "resend-rl@x.io", "password": PW})
    login = await client.post("/auth/login", json={"email": "resend-rl@x.io", "password": PW})
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    codes = [(await client.post("/auth/resend-verification", headers=h)).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429], codes
