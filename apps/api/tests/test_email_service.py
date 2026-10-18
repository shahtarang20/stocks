"""Tests for transactional email service (Resend, SMTP, and Console modes)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.services.email import (
    send_email,
    send_password_reset_email,
    send_verification_email,
)


@pytest.mark.asyncio
async def test_email_console_dispatch():
    """Verify that console dispatch succeeds without errors."""
    orig_provider = settings.EMAIL_PROVIDER
    try:
        settings.EMAIL_PROVIDER = "console"
        success = await send_verification_email(
            to_email="testuser@example.com",
            token="test-verification-token-12345",
            user_name="Alice",
        )
        assert success is True

        reset_success = await send_password_reset_email(
            to_email="testuser@example.com",
            token="test-reset-token-67890",
            user_name="Alice",
        )
        assert reset_success is True
    finally:
        settings.EMAIL_PROVIDER = orig_provider


@pytest.mark.asyncio
async def test_resend_missing_api_key_handled():
    """Verify graceful handling when Resend is configured without API key."""
    orig_provider = settings.EMAIL_PROVIDER
    orig_key = settings.RESEND_API_KEY
    try:
        settings.EMAIL_PROVIDER = "resend"
        settings.RESEND_API_KEY = ""
        success = await send_email(
            to_email="user@example.com",
            subject="Test",
            html_content="<p>Test</p>",
            text_content="Test",
        )
        assert success is False
    finally:
        settings.EMAIL_PROVIDER = orig_provider
        settings.RESEND_API_KEY = orig_key


@pytest.mark.asyncio
async def test_register_flow_sends_verification_email(client: AsyncClient, db_engine):
    """Verify user registration dispatches verification email and returns token in test mode."""
    res = await client.post(
        "/auth/register",
        json={"email": "emailtest_newuser@example.com", "password": "StrongPassword123!"},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["email"] == "emailtest_newuser@example.com"
    # In test environment, expose_dev_tokens is True
    assert data["email_verification_token"] is not None


@pytest.mark.asyncio
async def test_password_reset_request_sends_reset_email(client: AsyncClient, db_engine):
    """Verify password reset request dispatches email and returns token in test mode."""
    # First create user
    await client.post(
        "/auth/register",
        json={"email": "reset_recipient@example.com", "password": "StrongPassword123!"},
    )
    res = await client.post(
        "/auth/password-reset/request",
        json={"email": "reset_recipient@example.com"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "issued" in data["message"]
    assert data["password_reset_token"] is not None
