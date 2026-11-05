"""The Python and TypeScript SDKs must only call routes the API really serves.

Regression: both SDKs registered, logged in, refreshed and managed projects/API keys at /v1/auth/* and /v1/projects*,
which the API does not serve (it serves /auth/* and /projects*), so the quickstart in their READMEs failed with a 404.
"""

import pathlib
import re

from app.main import app

ROOT = pathlib.Path(__file__).resolve().parents[3] / "sdk"
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _norm(path: str) -> str:
    return re.sub(r"(\{[^}]*\}|\$\{[^}]*\})", "{}", path.split("?")[0])


def _routes() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for r in app.routes:
        if hasattr(r, "methods"):
            for m in r.methods or ():
                out.add((m, _norm(r.path)))
    return out


def _calls(src: str, quote: str) -> set[tuple[str, str]]:
    # ("POST", "/x"), ("POST", f"/x/{id}"), request("GET", `/x/${id}`)
    pat = rf'["\'](GET|POST|PUT|PATCH|DELETE)["\'],\s*f?{quote}(/[^{quote}]*){quote}'
    return {(m, _norm(p)) for m, p in re.findall(pat, src)}


def _check(calls: set[tuple[str, str]]) -> None:
    routes = _routes()
    assert ("POST", "/auth/login") in calls and ("POST", "/v1/jobs") in calls  # the extraction found the calls
    missing = sorted(c for c in calls if c not in routes)
    assert not missing, f"SDK calls routes the API does not serve: {missing}"


def test_python_sdk_paths_exist():
    src = (ROOT / "python" / "computecoin" / "client.py").read_text()
    _check(_calls(src, '"') | _calls(src, "'"))


def test_typescript_sdk_paths_exist():
    src = (ROOT / "typescript" / "src" / "client.ts").read_text()
    _check(_calls(src, '"') | _calls(src, "`"))
