"""Enrollment + config persistence tests."""

import stat

import pytest

from computecoin_agent.config import AgentConfig
from computecoin_agent.enrollment import enroll


def test_enroll_writes_0600_config(tmp_config_path):
    result = enroll("tok_machine_scoped_abc", config_path=tmp_config_path, provider_id="prov_1")
    assert result.config_path.exists()
    mode = stat.S_IMODE(result.config_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # Parent dir should be 0700.
    parent_mode = stat.S_IMODE(result.config_path.parent.stat().st_mode)
    assert parent_mode == 0o700, f"expected 0700, got {oct(parent_mode)}"


def test_enroll_persists_and_reloads(tmp_config_path):
    result = enroll("tok_xyz", config_path=tmp_config_path)
    loaded = AgentConfig.load(tmp_config_path)
    assert loaded.machine_id == result.machine_id
    assert loaded.enrollment_token == "tok_xyz"
    assert loaded.signing_secret
    assert len(loaded.signing_secret) == 64  # 32 bytes hex
    assert loaded.is_enrolled()


def test_enroll_generates_unique_secrets(tmp_config_path, tmp_path):
    other = tmp_path / "other" / "agent.toml"
    r1 = enroll("tok_a", config_path=tmp_config_path)
    r2 = enroll("tok_b", config_path=other)
    c1 = AgentConfig.load(tmp_config_path)
    c2 = AgentConfig.load(other)
    assert c1.signing_secret != c2.signing_secret
    assert r1.machine_id != r2.machine_id


def test_enroll_rejects_empty_token(tmp_config_path):
    with pytest.raises(ValueError):
        enroll("   ", config_path=tmp_config_path)


def test_env_override(monkeypatch, tmp_config_path):
    enroll("tok_env", config_path=tmp_config_path)
    monkeypatch.setenv("COMPUTECOIN_API_URL", "https://override.example")
    cfg = AgentConfig.load(tmp_config_path)
    assert cfg.api_url == "https://override.example"
