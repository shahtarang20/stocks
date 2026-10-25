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


_STRONG = dict(
    ENVIRONMENT="production",
    AUTH_SECRET="a-strong-auth-secret-value-0987654321",
    SESSION_SECRET="a-strong-session-secret-value-1234567890",
    PLATFORM_SIGNER_PRIVATE_KEY="0x" + "ab" * 32,
    SETTLEMENT_ENABLED=True,
)
_REAL_CHAIN = dict(
    SETTLEMENT_CHAIN_ID=56,
    SETTLEMENT_CONTRACT_ADDRESS="0x1111111111111111111111111111111111111111",
    COMPUTE_TOKEN_ADDRESS="0x2222222222222222222222222222222222222222",
    PLATFORM_TREASURY_ADDRESS="0x3333333333333333333333333333333333333333",
)


@pytest.mark.parametrize("name", ["SETTLEMENT_CONTRACT_ADDRESS", "COMPUTE_TOKEN_ADDRESS", "PLATFORM_TREASURY_ADDRESS"])
def test_prod_rejects_local_contract_addresses_when_settlement_enabled(name):
    from app.core.config import _DEV_CONTRACT_DEFAULTS

    with pytest.raises(Exception, match=name):
        Settings(**{**_STRONG, **_REAL_CHAIN, name: _DEV_CONTRACT_DEFAULTS[name]})


def test_prod_rejects_local_chain_id_when_settlement_enabled():
    with pytest.raises(Exception, match="SETTLEMENT_CHAIN_ID"):
        Settings(**{**_STRONG, **_REAL_CHAIN, "SETTLEMENT_CHAIN_ID": 31337})


def test_prod_boots_with_real_chain_settings():
    s = Settings(**{**_STRONG, **_REAL_CHAIN})
    assert s.SETTLEMENT_CHAIN_ID == 56


def test_contract_address_check_is_case_insensitive():
    with pytest.raises(Exception, match="COMPUTE_TOKEN_ADDRESS"):
        Settings(**{**_STRONG, **_REAL_CHAIN, "COMPUTE_TOKEN_ADDRESS": "0xE7F1725E7734CE288F8367E1BB143E90BB3F0512"})


@pytest.mark.parametrize("bad", ["", "0x123", "not-an-address", "0x" + "g" * 40])
def test_prod_rejects_blank_or_malformed_contract_address(bad):
    with pytest.raises(Exception, match="SETTLEMENT_CONTRACT_ADDRESS"):
        Settings(**{**_STRONG, **_REAL_CHAIN, "SETTLEMENT_CONTRACT_ADDRESS": bad})
