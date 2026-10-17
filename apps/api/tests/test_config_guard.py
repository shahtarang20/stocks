"""Audit fix C3 — the app refuses to boot in a non-dev ENVIRONMENT while still
using the shipped insecure default secrets / signer key."""

import pytest

from app.core.config import (
    Settings,
    _DEV_AUTH_SECRET,
    _DEV_SESSION_SECRET,
    _DEV_SIGNER_KEY,
)


def test_prod_rejects_default_auth_secret():
    with pytest.raises(Exception):
        Settings(
            ENVIRONMENT="production",
            AUTH_SECRET=_DEV_AUTH_SECRET,
            SESSION_SECRET="a-strong-session-secret-value-1234567890",
            SETTLEMENT_ENABLED=False,
        )


def test_prod_rejects_default_signer_when_settlement_enabled():
    with pytest.raises(Exception):
        Settings(
            ENVIRONMENT="production",
            AUTH_SECRET="a-strong-auth-secret-value-0987654321",
            SESSION_SECRET="a-strong-session-secret-value-1234567890",
            SETTLEMENT_ENABLED=True,
            PLATFORM_SIGNER_PRIVATE_KEY=_DEV_SIGNER_KEY,
        )


def test_prod_boots_with_strong_secrets():
    s = Settings(
        ENVIRONMENT="production",
        AUTH_SECRET="a-strong-auth-secret-value-0987654321",
        SESSION_SECRET="a-strong-session-secret-value-1234567890",
        SETTLEMENT_ENABLED=False,
    )
    assert s.ENVIRONMENT == "production"


def test_dev_environment_allows_defaults():
    # Local/dev/test convenience defaults remain usable in a dev environment
    # (no boot refusal), even with the insecure defaults explicitly set.
    s = Settings(
        ENVIRONMENT="development",
        AUTH_SECRET=_DEV_AUTH_SECRET,
        SESSION_SECRET=_DEV_SESSION_SECRET,
        PLATFORM_SIGNER_PRIVATE_KEY=_DEV_SIGNER_KEY,
        SETTLEMENT_ENABLED=True,
    )
    assert s.ENVIRONMENT == "development"
