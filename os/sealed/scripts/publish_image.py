#!/usr/bin/env python3
"""Publish the built image: POST /v1/privacy/trusted-images with the image hash and expected PCRs.
The API logs an IMAGE_HASH transparency-log entry before the TrustedImage row becomes usable.

Needs an administrator bearer token in $COMPUTECOIN_ADMIN_TOKEN (never a file in the repo, R10).
Auth: `get_current_user` (apps/api/app/deps.py) takes an explicit Bearer header before the web cookie, so an
administrator access token works here (checked in the source, not against a live API).

    publish_image.py --api https://api.example --expected out/expected-pcrs.json [--version-tag v1.2.0]
Exit status is non-zero on any refusal (409 image_hash_already_registered included).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def build_body(doc: dict, version_tag: str | None) -> dict:
    pcrs = doc.get("expected_pcrs") or {}
    if not pcrs:
        raise SystemExit("expected-pcrs.json has no expected_pcrs: refusing to publish an image any host would match (R1)")
    return {"image_hash": doc["image_hash"], "version_tag": version_tag or doc["version_tag"],
            "tier": doc.get("tier", "sealed"), "expected_pcrs": pcrs}


def publish(api: str, body: dict, token: str, opener=urllib.request.urlopen) -> dict:
    req = urllib.request.Request(api.rstrip("/") + "/v1/privacy/trusted-images", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="POST")
    try:
        with opener(req, timeout=30) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"publish refused: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')}") from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--expected", required=True)
    ap.add_argument("--version-tag")
    a = ap.parse_args(argv)
    token = os.environ.get("COMPUTECOIN_ADMIN_TOKEN")
    if not token:
        raise SystemExit("set COMPUTECOIN_ADMIN_TOKEN")
    body = build_body(json.load(open(a.expected)), a.version_tag)
    out = publish(a.api, body, token)
    print(json.dumps({"id": out.get("id"), "image_hash": out.get("image_hash"), "status": out.get("status")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
