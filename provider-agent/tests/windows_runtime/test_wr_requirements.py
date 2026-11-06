"""Requirements checker results (written, never run)."""

from __future__ import annotations

from computecoin_agent.windows.runtime.requirements_check import (
    FAIL, PASS, UNKNOWN, WARN, Probes, Thresholds, check_requirements,
)


def probes(**over) -> Probes:
    base = dict(cpu_arch=lambda: "AMD64", windows_version=lambda: (10, 0, 22631), windows_edition=lambda: "Core",
                virtualization=lambda: True, wsl_version=lambda: (2, 3, 26, 0), nvidia_driver=lambda: "552.44",
                free_disk_gb=lambda: 200.0, total_ram_gb=lambda: 32.0)
    base.update(over)
    return Probes(**base)


def by_id(report):
    return {r.id: r for r in report.results}


def test_all_good_passes():
    rep = check_requirements(probes())
    assert rep.ok and all(r.status == PASS for r in rep.results)
    assert {r.id for r in rep.results} == {"cpu_arch", "windows_version", "virtualization", "wsl", "nvidia_driver", "disk", "ram"}


def test_each_failure_blocks_and_has_a_plain_fix():
    cases = {
        "cpu_arch": dict(cpu_arch=lambda: "ARM64"),
        "windows_version": dict(windows_version=lambda: (10, 0, 18363)),
        "virtualization": dict(virtualization=lambda: False),
        "wsl": dict(wsl_version=lambda: None),
        "nvidia_driver": dict(nvidia_driver=lambda: "470.10"),
        "disk": dict(free_disk_gb=lambda: 10.0),
        "ram": dict(total_ram_gb=lambda: 4.0),
    }
    for rid, over in cases.items():
        rep = check_requirements(probes(**over))
        r = by_id(rep)[rid]
        assert r.status == FAIL and not rep.ok, rid
        assert r.message and r.fix, rid
        assert [b.id for b in rep.blocking] == [rid]


def test_old_wsl_fails_with_update_advice():
    r = by_id(check_requirements(probes(wsl_version=lambda: (1, 2, 5))))["wsl"]
    assert r.status == FAIL and "wsl --update" in r.fix


def test_missing_nvidia_driver_fails():
    r = by_id(check_requirements(probes(nvidia_driver=lambda: None)))["nvidia_driver"]
    assert r.status == FAIL and "NVIDIA" in r.message


def test_unknown_blocks_never_passes():
    rep = check_requirements(probes(virtualization=lambda: None, free_disk_gb=lambda: None))
    assert not rep.ok
    assert by_id(rep)["virtualization"].status == UNKNOWN and by_id(rep)["disk"].status == UNKNOWN


def test_a_probe_that_raises_is_unknown_not_a_pass():
    def boom():
        raise RuntimeError("wmi broke")

    rep = check_requirements(probes(total_ram_gb=boom))
    assert by_id(rep)["ram"].status == UNKNOWN and not rep.ok


def test_low_ram_is_a_warning_not_a_block():
    rep = check_requirements(probes(total_ram_gb=lambda: 12.0))
    assert by_id(rep)["ram"].status == WARN and rep.ok


def test_thresholds_are_configurable():
    rep = check_requirements(probes(nvidia_driver=lambda: "552.44"), Thresholds(min_nvidia_driver="560.0"))
    assert by_id(rep)["nvidia_driver"].status == FAIL


def test_json_shape_is_stable_for_the_website():
    d = check_requirements(probes(virtualization=lambda: False)).to_dict()
    assert d["schema"] == 1 and d["ok"] is False
    assert set(d["results"][0]) == {"id", "title", "status", "message", "fix", "detail"}
    assert "FAIL" in check_requirements(probes(virtualization=lambda: False)).summary()
