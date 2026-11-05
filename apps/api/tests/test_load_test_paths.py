"""tests/load/locustfile.py must only call routes the API really serves (it once logged in at /v1/auth/login, a 404)."""

import pathlib
import re

from app.main import app

LOCUST = pathlib.Path(__file__).resolve().parents[3] / "tests" / "load" / "locustfile.py"


def _routes() -> set[str]:
    return {re.sub(r"\{[^}]*\}", "{}", r.path) for r in app.routes if hasattr(r, "methods")}


def test_locustfile_paths_exist():
    src = LOCUST.read_text()
    paths = set(re.findall(r'(?:client\.(?:get|post)\(\s*f?|_post\(\s*)"(/[^"?]*)"', src))
    assert "/auth/login" in paths and "/v1/jobs" in paths  # the extraction found the calls
    routes = _routes()
    for p in paths:
        assert re.sub(r"\{[^}]*\}", "{}", p) in routes, f"locustfile calls {p}, which the API does not serve"
