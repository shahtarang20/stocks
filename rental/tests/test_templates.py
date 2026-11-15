"""Rental templates: the entrypoint, the Dockerfiles, the pinned requirements and the digest-filling script. No Docker needed."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "rental" / "templates"
SLUGS = ("pytorch-jupyter", "vision-jupyter")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


entry = _load(TEMPLATES / "_common" / "entrypoint.py", "rental_entrypoint")
fill = _load(ROOT / "scripts" / "rental_fill_digests.py", "rental_fill")
DIGEST = "a" * 64
IMAGE = f"ghcr.io/owner/rental-pytorch-jupyter@sha256:{DIGEST}"


# ---- the entrypoint ---------------------------------------------------------------------------------------------------
def test_jupyter_listens_on_loopback_only_with_the_workspace_as_root():
    cmd = entry.build_command({})
    assert cmd[:3] == ["jupyter", "lab", "--ip=127.0.0.1"] and "--port=8888" in cmd and "--no-browser" in cmd
    assert "--ServerApp.root_dir=/workspace" in cmd
    assert "--ServerApp.allow_root=False" in cmd and "--ServerApp.disable_check_xsrf=False" in cmd
    assert not any(a.startswith("--ip=0.0.0.0") for a in cmd)


def test_the_token_and_password_are_off_because_the_only_way_in_is_the_encrypted_channel():
    cmd = entry.build_command({})
    assert "--IdentityProvider.token=" in cmd and "--ServerApp.password=" in cmd


@pytest.mark.parametrize("value,expected", [
    ("/dashboard/sessions/abc-123/proxy/", "/dashboard/sessions/abc-123/proxy/"),
    ("/", "/"),
    ("", "/"),
    ("dashboard/proxy/", "/"),  # not rooted
    ("/dashboard/proxy", "/"),  # no trailing slash
    ("/a/../b/", "/"),  # no parent steps
    ("/a b/", "/"),
    ("/a;rm -rf/", "/"),
    ("/x/\n", "/"),
    ("/" + "a" * 300 + "/", "/"),
])
def test_the_base_path_is_only_used_when_it_is_a_plain_path(value, expected):
    assert entry.base_url({"COMPUTECOIN_SESSION_BASE_PATH": value}) == expected
    assert f"--ServerApp.base_url={expected}" in entry.build_command({"COMPUTECOIN_SESSION_BASE_PATH": value})


def test_everything_that_is_written_is_pointed_into_the_workspace():
    env = entry.child_env({"PATH": "/usr/bin", "HOME": "/root"})
    assert env["HOME"] == "/workspace"
    for key in ("JUPYTER_CONFIG_DIR", "JUPYTER_DATA_DIR", "JUPYTER_RUNTIME_DIR", "IPYTHONDIR", "MPLCONFIGDIR", "XDG_CACHE_HOME", "TORCH_HOME",
                "HF_HOME", "PIP_CACHE_DIR", "PYTHONUSERBASE", "TMPDIR"):
        assert env[key].startswith("/workspace/"), key
    assert env["PATH"].startswith("/workspace/.local/bin:") and env["PATH"].endswith("/usr/bin")


def test_the_environment_given_to_the_container_is_not_lost():
    assert entry.child_env({"PATH": "/x", "KEEP": "1"})["KEEP"] == "1"


# ---- the Dockerfiles --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("slug", SLUGS)
def test_the_base_image_is_pinned_by_digest_and_an_unpinned_one_fails_the_build(slug):
    text = (TEMPLATES / slug / "Dockerfile").read_text()
    default = re.search(r"^ARG BASE_IMAGE=(\S+)$", text, re.M)
    assert default and re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", default.group(1))
    assert 'case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest"' in text


@pytest.mark.parametrize("slug", SLUGS)
def test_the_image_has_the_attach_shim_the_entrypoint_and_no_root_or_open_port(slug):
    text = (TEMPLATES / slug / "Dockerfile").read_text()
    assert "COPY _common/nc_shim.py /usr/local/bin/nc" in text and "COPY _common/entrypoint.py /app/entrypoint.py" in text
    assert 'ENTRYPOINT ["python3", "/app/entrypoint.py"]' in text
    assert "EXPOSE" not in text  # no published port: the session channel is the only way in
    assert "chmod -R a+rX,a-w /app" in text  # the program is readable but not writable by the container user
    assert "ENV PYTHONPATH=/app" in text and "HOME=/workspace" in text


def test_both_templates_use_the_same_base_image():
    bases = {re.search(r"^ARG BASE_IMAGE=(\S+)$", (TEMPLATES / s / "Dockerfile").read_text(), re.M).group(1) for s in SLUGS}
    assert len(bases) == 1


def test_the_attach_shim_is_the_same_file_the_notebook_kit_ships():
    assert (TEMPLATES / "_common" / "nc_shim.py").read_bytes() == (ROOT / "kits" / "notebooks" / "nc_shim.py").read_bytes()


def test_the_vision_template_installs_facenet_pytorch_without_its_dependency_list_and_says_why():
    text = (TEMPLATES / "vision-jupyter" / "Dockerfile").read_text()
    assert "pip install --no-cache-dir --no-deps facenet-pytorch==2.6.0" in text and "pins an older torch" in text


# ---- the pinned requirements ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("slug", SLUGS)
def test_every_requirement_is_an_exact_version(slug):
    lines = [ln.strip() for ln in (TEMPLATES / slug / "requirements.txt").read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines
    for ln in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.]*", ln), ln


def test_the_vision_template_pins_numpy_and_pillow_for_facenet_pytorch():
    lines = (TEMPLATES / "vision-jupyter" / "requirements.txt").read_text().splitlines()
    assert "numpy==1.26.4" in lines and "Pillow==10.4.0" in lines


def test_torch_is_never_reinstalled_over_the_base_images():
    for slug in SLUGS:
        names = {ln.split("==")[0].lower() for ln in (TEMPLATES / slug / "requirements.txt").read_text().splitlines() if "==" in ln}
        assert not names & {"torch", "torchvision", "torchaudio"}


@pytest.mark.parametrize("slug", SLUGS)
def test_each_template_has_a_readme_that_covers_the_workspace_and_the_privacy_limit(slug):
    text = (TEMPLATES / slug / "README.md").read_text()
    assert "/workspace/results" in text and ("not** a private environment" in text or "pytorch-jupyter` README" in text)


# ---- the digest-filling script ----------------------------------------------------------------------------------------
def _file(tmp_path, templates=None):
    path = tmp_path / "rental_templates.json"
    path.write_text(json.dumps({"templates": templates if templates is not None else [
        {"slug": "pytorch-jupyter", "digest": None, "name": "x"}, {"slug": "vision-jupyter", "digest": None, "name": "y"}]}))
    return path


def test_the_script_sets_only_the_named_templates_digest(tmp_path):
    path = _file(tmp_path)
    assert fill.main(["--slug", "pytorch-jupyter", "--image", IMAGE, "--file", str(path)]) == 0
    data = json.loads(path.read_text())["templates"]
    assert data[0]["digest"] == IMAGE and data[0]["name"] == "x" and data[1]["digest"] is None


@pytest.mark.parametrize("image", [
    "ghcr.io/owner/x:latest", "ghcr.io/owner/x@sha256:abc", f"ghcr.io/Owner/x@sha256:{DIGEST}", f"evil.example.com/x@sha256:{DIGEST}",
    f"localhost/x@sha256:{DIGEST}", f"ghcr.io/owner/x@sha256:{'A' * 64}", "", f"ghcr.io/o/x@sha256:{DIGEST}\nx",
])
def test_an_image_that_is_not_pinned_lowercase_and_on_an_allowed_registry_is_refused(tmp_path, image):
    path = _file(tmp_path)
    before = path.read_text()
    with pytest.raises(SystemExit):
        fill.main(["--slug", "pytorch-jupyter", "--image", image, "--file", str(path)])
    assert path.read_text() == before  # nothing was written


def test_an_unknown_template_or_a_bad_name_is_refused(tmp_path):
    path = _file(tmp_path)
    for slug in ("nope", "../x", "UPPER", ""):
        with pytest.raises(SystemExit):
            fill.main(["--slug", slug, "--image", IMAGE, "--file", str(path)])


def test_a_broken_or_missing_file_is_refused_cleanly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    for target in (bad, tmp_path / "missing.json"):
        with pytest.raises(SystemExit):
            fill.main(["--slug", "pytorch-jupyter", "--image", IMAGE, "--file", str(target)])


def test_the_real_template_list_matches_the_template_folders():
    path = ROOT / "apps" / "api" / "app" / "data" / "rental_templates.json"
    if not path.exists():
        pytest.skip("the API builder has not written the template list yet")
    data = json.loads(path.read_text())
    items = data["templates"] if isinstance(data, dict) else data
    assert {t["slug"] for t in items} == set(SLUGS)
    for t in items:
        assert (TEMPLATES / t["slug"] / "Dockerfile").is_file()
        assert t.get("digest") is None or re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", t["digest"])
        assert t["mode"] == "tcp" and t["tcp_port"] == 8888 and not t.get("command")  # the image's own entrypoint runs Jupyter
