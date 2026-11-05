"""FRONTEND_URL may be a comma-separated CORS allow-list; email links must use only the first origin."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import email as email_service


def test_frontend_base_url_uses_first_origin(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com/, https://other.example.com")
    assert settings.frontend_base_url == "https://app.example.com"


@pytest.mark.asyncio
async def test_email_links_do_not_embed_origin_list(monkeypatch):
    sent: list[str] = []

    async def fake_send(to_email, subject, html_content, **kw):
        sent.append(html_content)
        return True

    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com,https://b.example.com")
    monkeypatch.setattr(email_service, "send_email", fake_send)
    await email_service.send_verification_email("u@example.com", "tok1")
    await email_service.send_password_reset_email("u@example.com", "tok2")
    joined = " ".join(sent)
    assert "https://app.example.com/verify-email?token=tok1" in joined
    assert "https://app.example.com/reset-password?token=tok2" in joined
    assert "b.example.com" not in joined
