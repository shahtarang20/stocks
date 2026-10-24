"""S3Store against a real S3-compatible endpoint (MinIO, moto server, S3).

S3Store used to be a stub whose every method raised, while docker-compose and the
example env both set OBJECT_STORAGE_ENDPOINT -- so a deployed stack could not
store a single job output, dataset or checkpoint. The default suite runs on the
local-filesystem store, so nothing exercised the S3 path.

Run:
    RUN_S3_INTEGRATION=1 S3_INTEGRATION_ENDPOINT=http://127.0.0.1:9000 \
    S3_INTEGRATION_ACCESS_KEY=minioadmin S3_INTEGRATION_SECRET_KEY=minioadmin \
    .venv/bin/pytest -q tests/test_storage_s3_live.py
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_S3_INTEGRATION", "").strip() in ("", "0", "false", "False"),
    reason="RUN_S3_INTEGRATION is not set; needs a real S3-compatible endpoint",
)


@pytest.fixture
def store():
    from app.services.storage import S3Store

    return S3Store(
        os.environ.get("S3_INTEGRATION_ENDPOINT", "http://127.0.0.1:9000"),
        f"cc-test-{uuid.uuid4().hex[:10]}",  # also proves the bucket is auto-created
        os.environ.get("S3_INTEGRATION_ACCESS_KEY", "minioadmin"),
        os.environ.get("S3_INTEGRATION_SECRET_KEY", "minioadmin"),
    )


def test_roundtrip_missing_key_and_delete(store):
    store.put("artifacts/j1/out.txt", b"hello", "text/plain")
    assert store.get("artifacts/j1/out.txt") == b"hello"

    with pytest.raises(FileNotFoundError):
        store.get("artifacts/j1/never-written")

    store.delete("artifacts/j1/out.txt")
    store.delete("artifacts/j1/out.txt")  # idempotent, like LocalFsStore
    with pytest.raises(FileNotFoundError):
        store.get("artifacts/j1/out.txt")


@pytest.mark.parametrize("bad", ["", "/abs", "../x", "a/../../b"])
def test_keys_cannot_escape(store, bad):
    with pytest.raises(ValueError):
        store.put(bad, b"x")
