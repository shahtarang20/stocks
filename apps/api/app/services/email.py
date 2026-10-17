"""Email delivery service supporting Resend, SMTP, and Console providers."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger("computecoin.email")


async def _send_via_resend(
    to_email: str,
    subject: str,
    html_content: str,
    text_content: str,
) -> bool:
    """Send transactional email via Resend REST API."""
    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not configured; cannot deliver email.")
        return False

    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "from": settings.EMAIL_FROM,
        "to": [to_email],
        "subject": subject,
        "html": html_content,
        "text": text_content,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code in {200, 201}:
                logger.info("Email delivered via Resend to %s (id: %s)", to_email, resp.json().get("id"))
                return True
            else:
                logger.error("Resend API failed [%d]: %s", resp.status_code, resp.text)
                return False
    except Exception as exc:
        logger.exception("Failed to dispatch email via Resend to %s: %s", to_email, exc)
        return False


def _send_via_smtp_sync(
    to_email: str,
    subject: str,
    html_content: str,
    text_content: str,
) -> bool:
    """Synchronous SMTP worker called inside a thread pool."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to_email
    msg.set_content(text_content)
    msg.add_alternative(html_content, subtype="html")

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10.0) as server:
            if settings.SMTP_TLS:
                server.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
            logger.info("Email delivered via SMTP to %s", to_email)
            return True
    except Exception as exc:
        logger.exception("SMTP email delivery failed to %s: %s", to_email, exc)
        return False


async def send_email(
    to_email: str,
    subject: str,
    html_content: str,
    text_content: str,
) -> bool:
    """Dispatch an email based on configured EMAIL_PROVIDER."""
    provider = settings.EMAIL_PROVIDER.lower().strip()

    if provider == "resend":
        return await _send_via_resend(to_email, subject, html_content, text_content)
    elif provider == "smtp":
        if not settings.SMTP_HOST:
            logger.error("SMTP_HOST not set; falling back to console logging.")
            return False
        return await asyncio.to_thread(
            _send_via_smtp_sync, to_email, subject, html_content, text_content
        )
    else:
        # Default / Console logging mode (Dev/Test)
        logger.info(
            "\n[EMAIL CONSOLE DISPATCH]\nTo: %s\nFrom: %s\nSubject: %s\nBody (Text):\n%s\n",
            to_email,
            settings.EMAIL_FROM,
            subject,
            text_content,
        )
        return True


def _build_html_template(title: str, preheader: str, body_html: str, cta_url: str, cta_text: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0b0f19; color: #e2e8f0; margin: 0; padding: 40px 20px; }}
    .container {{ max-width: 560px; margin: 0 auto; background-color: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 36px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    .logo {{ font-size: 20px; font-weight: bold; color: #38bdf8; margin-bottom: 24px; letter-spacing: -0.5px; }}
    .logo span {{ color: #ffffff; }}
    h1 {{ font-size: 22px; font-weight: 600; color: #f8fafc; margin-top: 0; margin-bottom: 16px; }}
    p {{ font-size: 15px; line-height: 1.6; color: #94a3b8; margin-bottom: 24px; }}
    .btn {{ display: inline-block; background-color: #0284c7; color: #ffffff !important; text-decoration: none; font-size: 15px; font-weight: 600; padding: 12px 28px; border-radius: 8px; margin-bottom: 28px; text-align: center; }}
    .btn:hover {{ background-color: #0369a1; }}
    .footer {{ font-size: 13px; color: #64748b; border-top: 1px solid #1f2937; padding-top: 20px; margin-top: 24px; }}
    .raw-link {{ word-break: break-all; color: #38bdf8; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="logo">⚡ Compute<span>Coin</span></div>
    <h1>{title}</h1>
    {body_html}
    <div style="text-align: center; margin: 32px 0;">
      <a href="{cta_url}" class="btn">{cta_text}</a>
    </div>
    <p style="font-size: 13px; color: #64748b;">
      If the button above doesn't work, copy and paste this link into your browser:<br>
      <a href="{cta_url}" class="raw-link">{cta_url}</a>
    </p>
    <div class="footer">
      This link is secure and time-limited. If you did not make this request, you can safely ignore this email.
    </div>
  </div>
</body>
</html>"""


async def send_verification_email(to_email: str, token: str, user_name: str | None = None) -> bool:
    """Send email verification link to user."""
    verify_url = f"{settings.FRONTEND_URL.rstrip('/')}/verify-email?token={token}"
    greeting = f"Hi {user_name}," if user_name else "Welcome to ComputeCoin,"

    title = "Verify your email address"
    body_html = f"""
    <p>{greeting}</p>
    <p>Thank you for signing up for ComputeCoin — the decentralized GPU marketplace. Please confirm your email address to activate your account and start deploying workloads.</p>
    """
    text_content = f"""{greeting}

Thank you for signing up for ComputeCoin. Please verify your email by opening the link below:

{verify_url}

This link will expire in {settings.EMAIL_VERIFICATION_TTL_SECONDS // 3600} hours.
"""
    html_content = _build_html_template(
        title=title,
        preheader="Confirm your email to get started on ComputeCoin",
        body_html=body_html,
        cta_url=verify_url,
        cta_text="Verify Email Address",
    )

    return await send_email(
        to_email=to_email,
        subject="Verify your email — ComputeCoin",
        html_content=html_content,
        text_content=text_content,
    )


async def send_password_reset_email(to_email: str, token: str, user_name: str | None = None) -> bool:
    """Send password reset link to user."""
    reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={token}"
    greeting = f"Hi {user_name}," if user_name else "Hello,"

    title = "Reset your password"
    body_html = f"""
    <p>{greeting}</p>
    <p>We received a request to reset the password for your ComputeCoin account. Click the button below to choose a new password.</p>
    """
    text_content = f"""{greeting}

We received a request to reset your password. Open the link below to choose a new password:

{reset_url}

This link is valid for {settings.PASSWORD_RESET_TTL_SECONDS // 60} minutes. If you did not request this, please disregard this email.
"""
    html_content = _build_html_template(
        title=title,
        preheader="Reset your ComputeCoin password",
        body_html=body_html,
        cta_url=reset_url,
        cta_text="Reset Password",
    )

    return await send_email(
        to_email=to_email,
        subject="Reset your password — ComputeCoin",
        html_content=html_content,
        text_content=text_content,
    )
