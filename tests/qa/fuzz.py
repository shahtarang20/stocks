"""Fuzz every JSON endpoint with hostile bodies; report anything that returns 5xx."""
import json, sys, uuid, httpx, itertools, collections
import os
BASE = os.environ.get("QA_API", "http://localhost:8010")
spec = httpx.get(BASE + "/openapi.json").json()
def login(e):
    return httpx.post(BASE + "/auth/login", json={"email": e, "password": "Password123!"}).json()["access_token"]
TOK = {"admin": login("admin@example.com"), "cust": login("cust1@example.com"), "prov": login("prov1@example.com")}
JUNK = [None, "", " ", "a" * 70000, -1, 0, 10**30, -10**30, 1.5, "NaN", "Infinity", "1e999", True, [], {}, [1, 2], {"a": {"b": []}}, "\u0000", "' OR 1=1 --", "<script>alert(1)</script>", "../../etc/passwd", str(uuid.uuid4()), "not-a-uuid", "2026-13-45T99:99:99Z", "😀" * 50]
def resolve(s):
    if "$ref" in s: return resolve(spec["components"]["schemas"][s["$ref"].split("/")[-1]])
    return s
def fields(schema):
    s = resolve(schema or {})
    return list((s.get("properties") or {}).keys())
fails = collections.defaultdict(list); n = 0
SKIP = ("/auth/logout", "/v1/billing/webhooks/stripe", "/v1/provider/connect/webhook", "/v1/agents/", "/v1/telemetry/job", "/v1/telemetry/machine", "/openapi", "/docs", "/redoc")
for path, ops in spec["paths"].items():
    if path.startswith(SKIP) or any(path.startswith(x) for x in SKIP): continue
    for method, op in ops.items():
        if method not in ("post", "put", "patch"): continue
        rb = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
        url = BASE + __import__("re").sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        bodies = [{}, [], "x", None, {"x": 1}]
        for f in fields(rb):
            bodies += [{f: j} for j in JUNK]
        for who in ("admin", "cust"):
            for body in bodies:
                n += 1
                try:
                    r = httpx.request(method.upper(), url, json=body, headers={"Authorization": "Bearer " + TOK[who]}, timeout=30)
                except Exception as e:
                    fails[f"{method.upper()} {path}"].append((who, "EXC " + str(e)[:60])); continue
                if r.status_code >= 500:
                    fails[f"{method.upper()} {path}"].append((who, json.dumps(body)[:70], r.status_code))
print("requests:", n)
print("endpoints returning 5xx:", len(fails))
for k, v in sorted(fails.items()):
    print(" ", k, "x%d" % len(v), v[0])
