"""B5: server validation of job network_policy (not yet executed)."""

import pytest

from app.services.network_policy import NetworkPolicyError, validate_network_policy


def test_valid_policy_is_normalized():
    out = validate_network_policy({"egress": ["Example.com:443", "example.com:443", "api.test.org:80"]})
    assert out == {"egress": ["api.test.org:80", "example.com:443"]}


@pytest.mark.parametrize("entry", [
    "127.0.0.1:80", "169.254.169.254:80", "10.0.0.1:443", "192.168.1.1:80", "2130706433:80",
    "0x7f000001:80", "localhost:80", "foo.localhost:80", "metadata.google.internal:80", "host.local:80",
    "[::1]:80", "example.com", "example.com:0", "example.com:70000", "*.example.com:443",
    "https://example.com:443", "user@example.com:443", "single:443",
])
def test_refused_entries(entry):
    with pytest.raises(NetworkPolicyError):
        validate_network_policy({"egress": [entry]})


def test_too_many_entries_and_privacy_tier():
    with pytest.raises(NetworkPolicyError):
        validate_network_policy({"egress": [f"h{i}.example.com:443" for i in range(17)]})
    with pytest.raises(NetworkPolicyError):
        validate_network_policy({"egress": ["example.com:443"]}, isolation_tier="sealed")


def test_default_is_none():
    assert validate_network_policy(None) is None
    assert validate_network_policy({"egress": []}) is None
