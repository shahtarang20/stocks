#!/usr/bin/env python3
"""Kit CI helper (U5). No secrets here; the signing key is never read by this script.

    python scripts/kit_ci.py check [--kits-dir kits]
    python scripts/kit_ci.py fill-digest --kit kits/ai-image/kit.json --repo ghcr.io/org/name --digest sha256:<64 hex> --out dist/ai-image.json
    python scripts/kit_ci.py publish --signed dist/ai-image.signed.json --api-url https://api.example --token-env KIT_ADMIN_TOKEN

`check` is the offline gate: manifest keys match the U1 schema (apps/api/app/services/kits.py), the image field is
either the 'PENDING' placeholder or a pinned digest, params.schema.json equals kit.json's param_schema, the Dockerfile
is pinned and non-root, the smoke spec names at least 3 GPU models (3 'cpu' host classes for a gpu_count 0 Kit) and its service/session steps are well formed, the verification method is a registered plug-in.
`fill-digest` writes a COPY of the manifest (never edits the committed file) with the real digest.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Keep identical to app/services/kits.py (validate_manifest).
ALLOWED = {"schema", "slug", "version", "name", "description", "category", "image", "mode", "param_schema",
           "inputs", "outputs", "resource_defaults", "billing_hints", "verification", "split", "egress", "assets",
           "checkpoint", "session", "service"}
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:]{0,200}@sha256:[0-9a-f]{64}$")
PLACEHOLDER_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:]{0,200}@sha256:PENDING$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
METHODS = {"exact", "perceptual", "psnr_ssim", "wer", "cosine", "loss_curve", "audio_spectral", "exact_deterministic"}  # keep in sync with app/services/verify_plugins
MIN_GPU_MODELS = 3
STEP_TYPES = ("service", "session")
CPU_LABEL_RE = re.compile(r"^cpu([-_.][a-z0-9._-]{0,40})?$")  # host class label of a CPU-only runner: "cpu", "cpu-a", "cpu-x86-big"
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}$")  # same shape as service_agent.safe_path
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
EXPECT_KEYS = ("status", "body_contains", "body_regex", "json")


def _is_cpu_label(label: str) -> bool:
    return bool(CPU_LABEL_RE.match(label.strip().lower()))


def host_classes(smoke: dict, cpu_only: bool) -> set[str]:
    """Distinct host classes a smoke spec names (``gpu_models_required`` + ``host_classes_required``). A GPU Kit counts
    only GPU model names; a CPU-only Kit (gpu_count 0) counts only 'cpu' labels, so a CPU Kit cannot claim GPU coverage."""
    raw = [*(smoke.get("gpu_models_required") or []), *(smoke.get("host_classes_required") or [])]
    names = {str(x).strip().lower() for x in raw if str(x).strip()}
    return {n for n in names if _is_cpu_label(n) == cpu_only}


def _expect_errors(expect: object, where: str, name: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(expect, dict) or not any(k in expect for k in EXPECT_KEYS):
        return [f"{name}: {where} needs expect with one of {list(EXPECT_KEYS)}"]
    for k in expect:
        if k not in EXPECT_KEYS:
            errs.append(f"{name}: {where} unknown expect key {k}")
    st = expect.get("status")
    if st is not None and not (isinstance(st, int) and not isinstance(st, bool)
                               or isinstance(st, list) and st and all(isinstance(x, int) and not isinstance(x, bool) for x in st)):
        errs.append(f"{name}: {where} expect.status must be an int or a non-empty list of ints")
    if "body_regex" in expect:
        try:
            re.compile(str(expect["body_regex"]))
        except re.error:
            errs.append(f"{name}: {where} expect.body_regex does not compile")
    for r in expect.get("json") or []:
        if not isinstance(r, dict) or not isinstance(r.get("path"), str) or not r["path"] \
                or not any(k in r for k in ("equals", "non_empty", "type")):
            errs.append(f"{name}: {where} expect.json rules need a path and one of equals, non_empty, type")
    return errs


def check_smoke_steps(smoke: dict, manifest: dict, name: str) -> list[str]:
    """Validate the optional ``steps`` of a smoke spec (schema: kits/README.md, "Smoke test spec")."""
    errs: list[str] = []
    steps = smoke.get("steps")
    has_session = isinstance(manifest.get("session"), dict)
    if steps is None:
        if has_session:
            errs.append(f"{name}: a session Kit's smoke spec needs a 'session' step (a batch run never exits)")
        return errs
    if not isinstance(steps, list) or not steps:
        return [f"{name}: smoke steps must be a non-empty list"]
    max_s = (smoke.get("limits") or {}).get("max_seconds")
    kinds: list[str] = []
    seen: set[str] = set()
    for i, st in enumerate(steps):
        w = f"steps[{i}]"
        if not isinstance(st, dict) or st.get("type") not in STEP_TYPES:
            errs.append(f"{name}: {w}.type must be one of {list(STEP_TYPES)}")
            continue
        kinds.append(st["type"])
        sid = st.get("id")
        if not isinstance(sid, str) or not ID_RE.match(sid) or sid in seen:
            errs.append(f"{name}: {w}.id must be a unique lowercase id")
        seen.add(str(sid))
        t = st.get("start_timeout_seconds") if st["type"] == "service" else st.get("ready_timeout_seconds")
        if t is not None and (not isinstance(t, int) or isinstance(t, bool) or t < 1 or (isinstance(max_s, int) and t > max_s)):
            errs.append(f"{name}: {w} timeout must be an int between 1 and limits.max_seconds")
        if st["type"] == "service":
            port = st.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                errs.append(f"{name}: {w}.port must be 1..65535")
            hp = st.get("health_path", "/health")
            if not isinstance(hp, str) or not SAFE_PATH_RE.match(hp) or ".." in hp or "//" in hp:
                errs.append(f"{name}: {w}.health_path must be a safe absolute path")
            reqs = st.get("requests")
            if not isinstance(reqs, list) or not reqs:
                errs.append(f"{name}: {w}.requests must be a non-empty list")
                continue
            rseen: set[str] = set()
            for j, rq in enumerate(reqs):
                rw = f"{w}.requests[{j}]"
                if not isinstance(rq, dict):
                    errs.append(f"{name}: {rw} must be an object")
                    continue
                rid = rq.get("id")
                if not isinstance(rid, str) or not ID_RE.match(rid) or rid in rseen:
                    errs.append(f"{name}: {rw}.id must be a unique lowercase id")
                rseen.add(str(rid))
                if rq.get("method", "GET") not in ("GET", "POST"):
                    errs.append(f"{name}: {rw}.method must be GET or POST")
                p = rq.get("path")
                if not isinstance(p, str) or not SAFE_PATH_RE.match(p) or ".." in p or "//" in p:
                    errs.append(f"{name}: {rw}.path must be a safe absolute path")
                errs.extend(_expect_errors(rq.get("expect"), rw, name))
        else:
            sess = manifest.get("session") if has_session else None
            if not isinstance(sess, dict) or sess.get("mode") != "tcp":
                errs.append(f"{name}: a session step needs kit.json session.mode 'tcp'")
            elif st.get("tcp_port", sess.get("tcp_port")) != sess.get("tcp_port"):
                errs.append(f"{name}: {w}.tcp_port must equal kit.json session.tcp_port")
            if not isinstance(st.get("request"), str) or not st["request"]:
                errs.append(f"{name}: {w}.request must be a non-empty string sent over the socket")
            ex = st.get("expect")
            if not isinstance(ex, dict) or not (ex.get("contains") or ex.get("regex")) or set(ex) - {"contains", "regex"}:
                errs.append(f"{name}: {w}.expect needs contains and/or regex (only those keys)")
            elif "regex" in ex:
                try:
                    re.compile(str(ex["regex"]))
                except re.error:
                    errs.append(f"{name}: {w}.expect.regex does not compile")
    if has_session and "session" not in kinds:
        errs.append(f"{name}: a session Kit's smoke spec needs a 'session' step")
    return errs


def check_kit(kit_dir: Path) -> list[str]:
    errs: list[str] = []
    name = kit_dir.name
    try:
        m = json.loads((kit_dir / "kit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{name}: kit.json unreadable: {exc}"]
    for k in m:
        if k not in ALLOWED:
            errs.append(f"{name}: unknown manifest key {k}")
    mode = m.get("mode")
    if mode not in ("batch", "service", "session"):
        errs.append(f"{name}: mode must be batch, service or session")
    if mode == "service":
        sv = m.get("service")
        if (not isinstance(sv, dict) or not isinstance(sv.get("port"), int) or isinstance(sv.get("port"), bool)
                or not 1 <= sv["port"] <= 65535 or not str(sv.get("health_path", "")).startswith("/")
                or set(sv) - {"port", "health_path", "max_concurrency"}):
            errs.append(f"{name}: mode service needs a valid service block {{port, health_path, max_concurrency?}}")
    elif "service" in m:
        errs.append(f"{name}: a service block needs mode service")
    if mode == "session" and "session" not in m:
        errs.append(f"{name}: mode session needs a session block")
    elif mode != "session" and "session" in m:
        errs.append(f"{name}: a session block needs mode session")
    if m.get("slug") != name:
        errs.append(f"{name}: slug must equal the directory name")
    if not (PLACEHOLDER_RE.match(str(m.get("image", ""))) or IMAGE_RE.match(str(m.get("image", "")))):
        errs.append(f"{name}: image must be name@sha256:PENDING or a pinned digest")
    try:
        ps = json.loads((kit_dir / "params.schema.json").read_text(encoding="utf-8"))
        if ps != m.get("param_schema"):
            errs.append(f"{name}: params.schema.json differs from kit.json param_schema")
    except (OSError, ValueError) as exc:
        errs.append(f"{name}: params.schema.json unreadable: {exc}")
    ps = m.get("param_schema") or {}
    if ps.get("type") != "object" or ps.get("additionalProperties") is not False:
        errs.append(f"{name}: param_schema must be an object schema with additionalProperties false")
    if "$ref" in json.dumps(ps):
        errs.append(f"{name}: param_schema must not use $ref")
    for o in m.get("outputs", []):
        if not str(o.get("path", "")).startswith("/out/"):
            errs.append(f"{name}: output path must be under /out/")
    if (m.get("verification") or {}).get("method") not in METHODS:
        errs.append(f"{name}: verification.method must be one of {sorted(METHODS)}")
    df = (kit_dir / "Dockerfile")
    text = df.read_text(encoding="utf-8") if df.exists() else ""
    if not text:
        errs.append(f"{name}: Dockerfile missing")
    else:
        if not re.search(r"^USER\s+10001\s*$", text, re.M):
            errs.append(f"{name}: Dockerfile must end as non-root (USER 10001)")
        if "must be pinned by digest" not in text or "ARG BASE_IMAGE" not in text:
            errs.append(f"{name}: Dockerfile must require a digest-pinned BASE_IMAGE")
        if re.search(r"^\s*ADD\s+https?://", text, re.M):
            errs.append(f"{name}: no ADD of remote URLs; download with a hash check")
        if re.search(r"^FROM\s+[^$\s]+:latest", text, re.M):
            errs.append(f"{name}: no :latest base")
    try:
        smoke = json.loads((kit_dir / "smoke" / "smoke.json").read_text(encoding="utf-8"))
        gpus = (m.get("resource_defaults") or {}).get("gpu_count")
        cpu_only = isinstance(gpus, int) and not isinstance(gpus, bool) and gpus == 0
        models = host_classes(smoke, cpu_only)
        if len(models) < MIN_GPU_MODELS:
            errs.append(f"{name}: smoke spec must name at least {MIN_GPU_MODELS} distinct "
                        + ("CPU host classes ('cpu' labels, host_classes_required)" if cpu_only else "GPU models"))
        errs.extend(check_smoke_steps(smoke, m, name))
        if smoke.get("kit") != m.get("slug") or not smoke.get("checks"):
            errs.append(f"{name}: smoke spec must name the kit and list checks")
    except (OSError, ValueError) as exc:
        errs.append(f"{name}: smoke/smoke.json unreadable: {exc}")
    for f in ("README.md", "entrypoint.py"):
        if not (kit_dir / f).exists():
            errs.append(f"{name}: {f} missing")
    return errs


def cmd_check(a) -> int:
    root = Path(a.kits_dir)
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and (p / "kit.json").exists())
    if not dirs:
        print("no kits found", file=sys.stderr)
        return 1
    errs = [e for d in dirs for e in check_kit(d)]
    for e in errs:
        print("ERROR " + e, file=sys.stderr)
    print(f"checked {len(dirs)} kit(s): {len(errs)} problem(s)")
    return 1 if errs else 0


def cmd_fill(a) -> int:
    if not DIGEST_RE.match(a.digest):
        sys.exit("digest must be sha256:<64 lowercase hex>")
    repo = a.repo.lower()
    if "@" in repo or ":" in repo.split("/")[-1]:
        sys.exit("--repo must be a repository name without tag or digest")
    m = json.loads(Path(a.kit).read_text(encoding="utf-8"))
    m["image"] = f"{repo}@{a.digest}"
    if not IMAGE_RE.match(m["image"]):
        sys.exit("resulting image reference is not valid for the Kit registry")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    print(m["image"])
    return 0


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would forward the Authorization header to wherever a redirect points: refuse every redirect instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused (the admin token is never forwarded)", headers, fp)


_NO_REDIRECT = urllib.request.build_opener(_RefuseRedirect)


def _publish_url_ok(url: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


def cmd_publish(a) -> int:
    token = os.environ.get(a.token_env, "")
    if not token:
        sys.exit(f"environment variable {a.token_env} is empty")
    url = a.api_url.rstrip("/") + "/v1/admin/kits"
    if not _publish_url_ok(url):
        sys.exit("--api-url must be https:// (http:// only for localhost): the admin token is sent as a bearer header")
    body = Path(a.signed).read_bytes()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with _NO_REDIRECT.open(req, timeout=60) as r:
            print(r.status, r.read().decode()[:500])
    except urllib.error.HTTPError as exc:
        print(exc.code, exc.read().decode()[:1000], file=sys.stderr)
        return 1 if exc.code != 409 else 0  # 409: this version is already published; versions are immutable
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--kits-dir", default="kits")
    f = sub.add_parser("fill-digest")
    f.add_argument("--kit", required=True)
    f.add_argument("--repo", required=True)
    f.add_argument("--digest", required=True)
    f.add_argument("--out", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--signed", required=True)
    p.add_argument("--api-url", required=True)
    p.add_argument("--token-env", default="KIT_ADMIN_TOKEN")
    a = ap.parse_args()
    return {"check": cmd_check, "fill-digest": cmd_fill, "publish": cmd_publish}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
