"""llm-serve, notebooks and BYOC template checks (WRITTEN, NOT RUN). Run from repo root: pytest kits/tests
Pure Python; vLLM, Jupyter, docker and GPUs are not needed. The BYOC checks need pydantic (the API environment).
Bug re-insertion list: make llm-serve bind 127.0.0.1 (bind test fails); change the notebooks entrypoint command without
kit.json (match test fails); drop the digest check in check_byoc_image_spec (tag test fails); allow a non-loopback host in
nc_shim.parse_args (shim test fails).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KITS = ROOT / "kits"
sys.path.insert(0, str(KITS / "_common"))
sys.path.insert(0, str(ROOT / "scripts"))

import kit_ci  # noqa: E402
import kitrt  # noqa: E402

SLUGS = ["llm-serve", "notebooks"]
DIGEST = "sha256:" + "ab" * 32  # test-only


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest(slug):
    return json.loads((KITS / slug / "kit.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("slug", SLUGS)
def test_offline_gate_and_pattern(slug):
    assert kit_ci.check_kit(KITS / slug) == []
    m = _manifest(slug)
    assert m["image"].endswith("@sha256:PENDING")
    assert m["param_schema"]["additionalProperties"] is False
    assert kitrt.validate({}, m["param_schema"]) == {}
    with pytest.raises(kitrt.KitError):
        kitrt.validate({"x": 1}, m["param_schema"])
    df = (KITS / slug / "Dockerfile").read_text(encoding="utf-8")
    assert "must be pinned by digest" in df and re.search(r"^USER 10001$", df, re.M)


def test_llm_serve_matches_service_agent_contract():
    ep = _load(KITS / "llm-serve" / "entrypoint.py", "ep_llm_serve")
    argv = ep.build_argv("/opt/weights/model")
    assert argv[argv.index("--port") + 1] == "8000"  # service_agent default port
    assert argv[argv.index("--host") + 1] == "0.0.0.0"  # reached by container IP; loopback would be unreachable
    assert ep.HEALTH_PATH == "/health"
    assert "--trust-remote-code" not in argv and "--api-key" not in argv
    env = ep.child_env({"PATH": "/usr/bin"})
    assert env["HF_HUB_OFFLINE"] == "1" and env["VLLM_NO_USAGE_STATS"] == "1" and env["HOME"] == "/tmp"
    assert env["PATH"] == "/usr/bin"
    df = (KITS / "llm-serve" / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 8000" in df
    m = _manifest("llm-serve")
    assert m["mode"] == "service" and m["category"] == "inference" and "session" not in m
    assert m["service"]["port"] == 8000 and m["service"]["health_path"] == ep.HEALTH_PATH


def test_llm_serve_refuses_missing_weights(monkeypatch, tmp_path):
    ep = _load(KITS / "llm-serve" / "entrypoint.py", "ep_llm_serve2")
    monkeypatch.setattr(ep, "WEIGHTS", str(tmp_path))
    with pytest.raises(kitrt.KitError) as exc:
        ep.main()
    assert exc.value.code == "bad_asset_manifest"


def test_notebooks_session_block_matches_entrypoint_and_agent():
    m = _manifest("notebooks")
    ep = _load(KITS / "notebooks" / "entrypoint.py", "ep_notebooks")
    block = m["session"]
    assert m["mode"] == "session" and "service" not in m
    assert block["mode"] == "tcp" and block["tcp_port"] == 8888
    assert block["command"] == ep.build_command()
    cmd = ep.build_command()
    assert "--ip=127.0.0.1" in cmd and "--port=8888" in cmd and "--IdentityProvider.token=" in cmd
    assert ep.child_env({})["JUPYTER_RUNTIME_DIR"].startswith("/tmp/")
    assert ep.WORKSPACE.startswith("/tmp/")
    assert all(not isinstance(x, (dict, list)) for x in cmd)


def test_notebooks_attach_command_uses_declared_port():
    pytest.importorskip("httpx", reason="session_agent imports its transport lazily; skip when the agent deps are missing")
    sys.path.insert(0, str(ROOT / "provider-agent"))
    from computecoin_agent.session_agent import build_attach_argv

    block = _manifest("notebooks")["session"]
    argv = build_attach_argv({"id": "s1", "mode": block["mode"], "tcp_port": block["tcp_port"]})
    assert argv[-3:] == ["nc", "127.0.0.1", "8888"]


def test_nc_shim_only_accepts_loopback():
    shim = _load(KITS / "notebooks" / "nc_shim.py", "nc_shim")
    assert shim.parse_args(["127.0.0.1", "8888"]) == ("127.0.0.1", 8888)
    for bad in (["10.0.0.1", "80"], ["127.0.0.1"], ["127.0.0.1", "0"], ["127.0.0.1", "x"], ["127.0.0.1", "70000"]):
        with pytest.raises(SystemExit):
            shim.parse_args(bad)


# ---- BYOC template and validator ----------------------------------------------------------------------------

def _byoc():
    pytest.importorskip("pydantic")
    sys.path.insert(0, str(ROOT / "apps" / "api"))
    return _load(ROOT / "scripts" / "check_byoc_image_spec.py", "check_byoc")


def _spec(**over):
    s = json.loads((KITS / "byoc-template" / "example-spec.json").read_text(encoding="utf-8"))
    s.update(over)
    return s


def test_byoc_example_spec_is_accepted():
    mod = _byoc()
    errors, warnings = mod.check_spec(_spec())
    assert errors == []
    assert mod.check_spec({"spec": _spec()})[0] == []  # the POST body shape


def test_byoc_rejects_tag_unknown_field_and_bad_env():
    mod = _byoc()
    assert mod.check_spec(_spec(image="registry.example.com/acme/tool:latest"))[0]
    assert mod.check_spec(_spec(privileged=True))[0]  # no field for it: unknown fields are refused
    assert mod.check_spec(_spec(isolation="container"))[0]
    assert mod.check_spec(_spec(outputs=[{"name": "r", "path": "../etc"}]))[0]
    assert mod.check_spec(_spec(env={"bad": "x"}))[0]
    errors, warnings = mod.check_spec(_spec(env={"COMPUTECOIN_X": "1"}))
    assert errors == [] and any("reserved" in w for w in warnings)
    errors, _ = mod.check_spec(_spec(), ["other.example.com"])
    assert errors


def test_byoc_dockerfile_lint(tmp_path):
    mod = _byoc()
    good = (KITS / "byoc-template" / "Dockerfile").read_text(encoding="utf-8")
    assert mod.lint_dockerfile(good) == []
    bad = "FROM python:latest\nENTRYPOINT python run.py\n"
    problems = mod.lint_dockerfile(bad)
    assert len(problems) >= 3


def test_byoc_template_is_not_a_kit():
    assert not (KITS / "byoc-template" / "kit.json").exists()
