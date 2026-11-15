"""Container rental: the host's opt-in setting, the capability flag it produces, and the safe settings-file updater."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from computecoin_agent import capabilities as caps
from computecoin_agent.config import AgentConfig, update_config_file
from computecoin_agent.platform import PLATFORM_WINDOWS
from computecoin_agent.platform.fake import make_fake_platform


def _cfg(**kw):
    base = dict(gpu_power_limit_w=None, gpu_power_limit_percent=None, egress_proxy_image=None, cpu_only_kits_enabled=False,
                multinode_network=None, multinode_network_firewalled=False, allow_container_rental=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _report():
    return SimpleNamespace(gpus=[], isolation_tiers=["container"], contract_summary=lambda: {})


def _flags(config, plat=None):
    return caps.compute_capabilities(_report(), config=config, plat=plat or make_fake_platform(),
                                     isolation_runtimes_fn=lambda: [], vfio_fn=lambda: None)


# ---- the flag -------------------------------------------------------------------------------------------------------
def test_the_flag_is_reported_only_after_the_host_opts_in():
    assert "container_rental" not in _flags(_cfg())
    assert "container_rental" in _flags(_cfg(allow_container_rental=True))


@pytest.mark.parametrize("lookalike", ["true", 1, "yes", [], {}, None])
def test_only_a_real_true_counts(lookalike):
    assert "container_rental" not in _flags(_cfg(allow_container_rental=lookalike))


def test_no_container_engine_means_no_flag_even_when_opted_in():
    plat = make_fake_platform(docker_available=False)
    assert "container_rental" not in _flags(_cfg(allow_container_rental=True), plat)


def test_a_windows_host_never_reports_it():
    plat = make_fake_platform(PLATFORM_WINDOWS)
    assert "container_rental" not in _flags(_cfg(allow_container_rental=True), plat)
    assert "container_rental" in caps.WINDOWS_FORBIDDEN_FLAGS


def test_the_flag_comes_last_in_the_stable_order():
    assert caps.KNOWN_CAPABILITY_FLAGS[-1] == "container_rental"
    out = _flags(_cfg(allow_container_rental=True, cpu_only_kits_enabled=True))
    assert out == [f for f in caps.KNOWN_CAPABILITY_FLAGS if f in out] and out[-1] == "container_rental"


# ---- the settings ---------------------------------------------------------------------------------------------------
def test_defaults_are_safe():
    c = AgentConfig()
    assert c.allow_container_rental is False and c.serve_sessions is True
    assert c.rental_root is None and c.rental_max_workspace_gb == 50 and c.rental_max_memory_gb == 0 and c.rental_max_cpus == 0


@pytest.mark.parametrize("kw", [
    {"rental_max_workspace_gb": 0}, {"rental_max_workspace_gb": 10_001}, {"rental_max_memory_gb": -1},
    {"rental_max_memory_gb": 4097}, {"rental_max_cpus": -1}, {"rental_max_cpus": 1025},
    {"rental_root": "relative/dir"}, {"rental_root": "/"}, {"rental_root": "/x\x00y"},
])
def test_bad_values_are_refused(kw):
    with pytest.raises(ValueError):
        AgentConfig(**kw)


def test_good_values_and_an_empty_root_are_accepted():
    c = AgentConfig(rental_max_workspace_gb=200, rental_max_memory_gb=64, rental_max_cpus=12, rental_root="/srv/rentals")
    assert (c.rental_max_workspace_gb, c.rental_max_memory_gb, c.rental_max_cpus, c.rental_root) == (200, 64, 12, "/srv/rentals")
    assert AgentConfig(rental_root="  ").rental_root is None


# ---- the settings-file updater --------------------------------------------------------------------------------------
def test_the_updater_changes_only_the_named_keys_and_keeps_the_file_private(tmp_path):
    import os
    import stat
    import tomllib

    f = tmp_path / "cfg" / "agent.toml"
    f.parent.mkdir()
    f.write_text('machine_id = "m1"\nmachine_secret = "s3cret"\nhost_kind = "dedicated"\n')
    update_config_file({"allow_container_rental": True}, f)
    data = tomllib.loads(f.read_text())
    assert data == {"machine_id": "m1", "machine_secret": "s3cret", "host_kind": "dedicated", "allow_container_rental": True}
    if os.name == "posix":
        assert stat.S_IMODE(f.stat().st_mode) & 0o077 == 0  # not readable by anyone else


def test_the_updater_creates_a_missing_file_and_does_not_freeze_defaults(tmp_path):
    import tomllib

    f = tmp_path / "new" / "agent.toml"
    update_config_file({"allow_container_rental": False}, f)
    assert tomllib.loads(f.read_text()) == {"allow_container_rental": False}


def test_the_updater_refuses_bad_values_before_writing_and_refuses_a_broken_file(tmp_path):
    f = tmp_path / "agent.toml"
    f.write_text('machine_id = "m1"\n')
    with pytest.raises(ValueError):
        update_config_file({"rental_max_workspace_gb": 0}, f)
    assert f.read_text() == 'machine_id = "m1"\n'
    f.write_text("this is = = not toml [[")
    with pytest.raises(ValueError):
        update_config_file({"allow_container_rental": True}, f)
    assert f.read_text() == "this is = = not toml [["  # an unreadable file is left untouched
