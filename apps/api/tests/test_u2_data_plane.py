"""U2 data plane. WRITTEN, NOT YET RUN (the task that added them forbade running tests).

A fake S3-compatible store (in memory) stands in for the vendor. It follows the documented S3 behaviour:
a part uploaded with a checksum header is rejected when its bytes differ; ListParts reports the checksum
S3 computed for what actually arrived; CompleteMultipartUpload concatenates parts. Real S3/R2/MinIO runs
are still to do (see docs/architecture/U2-data-plane-design.md, section Tests).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.data_plane import DataFile, DataObject, DataUpload
from app.services import data_plane as svc
from app.services.data_plane_store import (
    DataPlaneStore,
    ScopedCredentials,
    StoredPart,
    ByoClient,
    check_key,
    set_byo_client,
    set_data_store,
)
from tests.helpers import auth_headers, get_user_id, register_verify_login

pytestmark = pytest.mark.asyncio
MIB = 1024 * 1024


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class FakeStore(DataPlaneStore):
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict] = {}
        self.read_calls = 0

    def create_multipart(self, key, content_type):
        check_key(key)
        uid = uuid.uuid4().hex
        self.uploads[uid] = {"key": key, "parts": {}}
        return uid

    def presign_upload_part(self, key, upload_id, part_number, sha256_hex, size_bytes, ttl_seconds):
        assert ttl_seconds > 0
        return f"fake://{upload_id}/{part_number}", {"x-amz-checksum-sha256": sha256_hex}

    # What the client does with the URL. enforce=True mimics S3 rejecting a mismatching body.
    def client_put(self, upload_id, n, data, claimed_sha, enforce=True):
        if enforce and sha(data) != claimed_sha:
            raise IOError("BadDigest")
        self.uploads[upload_id]["parts"][n] = data

    def list_parts(self, key, upload_id):
        return [
            StoredPart(n, len(d), "etag-" + sha(d)[:8], sha(d)) for n, d in sorted(self.uploads[upload_id]["parts"].items())
        ]

    def complete_multipart(self, key, upload_id, parts):
        up = self.uploads.pop(upload_id)
        self.objects[key] = b"".join(up["parts"][n] for n, _ in parts)

    def abort_multipart(self, key, upload_id):
        self.uploads.pop(upload_id, None)

    def head_size(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def sha256_of(self, key):
        self.read_calls += 1
        return sha(self.objects[key])

    def copy(self, src_key, dst_key):
        self.objects[dst_key] = self.objects[src_key]

    def delete(self, key):
        self.objects.pop(key, None)

    def presign_download(self, key, ttl_seconds, filename, content_type):
        return f"fake-get://{key}?ttl={ttl_seconds}"


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    set_data_store(s)
    for k, v in dict(
        DATA_PLANE_ENABLED=True, DATA_PLANE_ENDPOINT="https://s3.example", DATA_PLANE_BUCKET="b",
        DATA_PLANE_ACCESS_KEY="k", DATA_PLANE_SECRET_KEY_ID="s", DATA_PLANE_PART_SIZE_BYTES=5 * MIB,
        DATA_PLANE_ACCOUNT_QUOTA_BYTES=10**12, DATA_PLANE_MAX_FILE_BYTES=10**12, DATA_PLANE_GC_GRACE_SECONDS=0,
        DATA_PLANE_RETENTION_DEFAULT_DAYS=0, DATA_PLANE_RETENTION_MAX_DAYS=0, DATA_PLANE_CROSS_ACCOUNT_DEDUP=False,
        DATA_PLANE_INLINE_VERIFY_MAX_BYTES=10**9, DATA_PLANE_BYO_ENABLED=True,
        DATA_PLANE_CREDENTIAL_KEY="k" * 40,
    ).items():
        monkeypatch.setattr(settings, k, v)
    yield s
    set_data_store(None)
    set_byo_client(None)


async def _owner(client, email):
    await register_verify_login(client, email)
    return await get_user_id(email)


def _payload(n_parts_extra=3):
    # 2 full 5 MiB parts + a short tail
    return os.urandom(10 * MIB + n_parts_extra)


async def _upload(db, store, owner, data, key=None, name="f.bin", corrupt_part=None, internal_name=False):
    up, file = await svc.create_upload(
        db, owner_id=owner, idempotency_key=key or uuid.uuid4().hex, name=name, content_type="application/octet-stream",
        sha256=sha(data), size_bytes=len(data), internal_name=internal_name,
    )
    if file is not None:
        return up, file
    for n in range(1, up.part_count + 1):
        chunk = data[(n - 1) * up.part_size : n * up.part_size]
        info = await svc.issue_part_url(db, up, part_number=n, sha256=sha(chunk), size_bytes=len(chunk))
        sid = info["url"].split("/")[-2].replace("fake://", "")
        store.client_put(up.store_upload_id, n, chunk, sha(chunk))
        await svc.confirm_part(db, up, part_number=n)
    return await svc.complete_upload(db, up)


async def test_u2_1_resume_after_dropped_connection(client, store):
    owner = await _owner(client, "u2a@example.com")
    data = _payload()
    async with get_sessionmaker()() as db:
        up, _ = await svc.create_upload(
            db, owner_id=owner, idempotency_key="k1", name="big", content_type="application/zip",
            sha256=sha(data), size_bytes=len(data),
        )
        assert up.part_count == 3
        # Only part 1 gets through, then the connection drops.
        c1 = data[: up.part_size]
        await svc.issue_part_url(db, up, part_number=1, sha256=sha(c1), size_bytes=len(c1))
        store.client_put(up.store_upload_id, 1, c1, sha(c1))
        await svc.confirm_part(db, up, part_number=1)
        await db.commit()
    async with get_sessionmaker()() as db:  # a new process / a new day
        up = await svc.get_owned_upload(db, owner, up.id)
        st = await svc.upload_state(db, up)
        assert st["verified_parts"] == [1] and st["missing_parts"] == [2, 3]
        with pytest.raises(svc.DataPlaneError) as e:
            await svc.complete_upload(db, up)
        assert e.value.code == "parts_missing"
        again = await svc.issue_part_url(db, up, part_number=1, sha256=sha(c1), size_bytes=len(c1))
        assert again["already_uploaded"] is True  # part 1 is not sent again
        for n in (2, 3):
            ch = data[(n - 1) * up.part_size : n * up.part_size]
            await svc.issue_part_url(db, up, part_number=n, sha256=sha(ch), size_bytes=len(ch))
            store.client_put(up.store_upload_id, n, ch, sha(ch))
            await svc.confirm_part(db, up, part_number=n)
        up, file = await svc.complete_upload(db, up)
        assert up.status == "complete" and file.sha256 == sha(data)
        await db.commit()


async def test_u2_2_corrupt_part_detected_and_resent(client, store):
    owner = await _owner(client, "u2b@example.com")
    data = _payload()
    async with get_sessionmaker()() as db:
        up, _ = await svc.create_upload(
            db, owner_id=owner, idempotency_key="k2", name="x", content_type="application/zip",
            sha256=sha(data), size_bytes=len(data),
        )
        for n in range(1, up.part_count + 1):
            ch = data[(n - 1) * up.part_size : n * up.part_size]
            await svc.issue_part_url(db, up, part_number=n, sha256=sha(ch), size_bytes=len(ch))
            # Part 2 is damaged in flight and the store (misbehaving) accepts it.
            body = ch[:-1] + bytes([ch[-1] ^ 1]) if n == 2 else ch
            store.client_put(up.store_upload_id, n, body, sha(ch), enforce=False)
            part = await svc.confirm_part(db, up, part_number=n)
            assert (part.status == "corrupt") == (n == 2)
        with pytest.raises(svc.DataPlaneError) as e:
            await svc.complete_upload(db, up)
        assert e.value.code == "parts_not_verified"
        ch = data[up.part_size : 2 * up.part_size]
        await svc.issue_part_url(db, up, part_number=2, sha256=sha(ch), size_bytes=len(ch))  # new URL for the re-send
        store.client_put(up.store_upload_id, 2, ch, sha(ch))
        assert (await svc.confirm_part(db, up, part_number=2)).status == "verified"
        up, file = await svc.complete_upload(db, up)
        assert up.status == "complete"


async def test_whole_file_hash_mismatch_is_refused(client, store):
    owner = await _owner(client, "u2c@example.com")
    data = _payload()
    async with get_sessionmaker()() as db:
        up, _ = await svc.create_upload(
            db, owner_id=owner, idempotency_key="k3", name="x", content_type="application/zip",
            sha256=sha(b"something else"), size_bytes=len(data),  # the claim is wrong
        )
        for n in range(1, up.part_count + 1):
            ch = data[(n - 1) * up.part_size : n * up.part_size]
            await svc.issue_part_url(db, up, part_number=n, sha256=sha(ch), size_bytes=len(ch))
            store.client_put(up.store_upload_id, n, ch, sha(ch))
            await svc.confirm_part(db, up, part_number=n)
        up, file = await svc.complete_upload(db, up)
        assert up.status == "failed" and up.error_code == "hash_mismatch" and file is None
        assert not store.objects  # staging deleted, nothing addressable
        assert (await db.execute(select(func.count()).select_from(DataObject))).scalar_one() == 0


async def test_u2_3_same_file_twice_stored_once(client, store):
    owner = await _owner(client, "u2d@example.com")
    data = _payload()
    async with get_sessionmaker()() as db:
        _, f1 = await _upload(db, store, owner, data, name="a.bin")
        reads_after_first = store.read_calls
        up2, f2 = await _upload(db, store, owner, data, name="b.bin")  # instant dedup, no bytes
        assert up2.store_upload_id == "dedup" and f1.id != f2.id and f1.object_id == f2.object_id
        assert store.read_calls == reads_after_first
        assert len([k for k in store.objects if k.startswith("cas/")]) == 1
        obj = (await db.execute(select(DataObject))).scalar_one()
        assert obj.refcount == 2


async def test_no_cross_account_dedup_by_default(client, store):
    a = await _owner(client, "u2e@example.com")
    b = await _owner(client, "u2f@example.com")
    data = _payload()
    async with get_sessionmaker()() as db:
        await _upload(db, store, a, data)
        up, f = await _upload(db, store, b, data)
        assert up.store_upload_id != "dedup"  # no hash oracle: B must send the bytes
        assert len([k for k in store.objects if k.startswith("cas/")]) == 2


async def test_delete_releases_reference_and_gc_removes_object(client, store):
    owner = await _owner(client, "u2g@example.com")
    data = _payload()
    async with get_sessionmaker()() as db:
        _, f1 = await _upload(db, store, owner, data, name="a")
        _, f2 = await _upload(db, store, owner, data, name="b")
        await svc.delete_file(db, owner_id=owner, file_id=f1.id)
        await svc.sweep_data_plane(db)
        assert any(k.startswith("cas/") for k in store.objects)  # still referenced by f2
        await svc.delete_file(db, owner_id=owner, file_id=f2.id)
        out = await svc.sweep_data_plane(db)
        assert out["objects_deleted"] == 1 and not any(k.startswith("cas/") for k in store.objects)
        with pytest.raises(svc.DataPlaneError):
            await svc.create_download_link(db, owner_id=owner, file_id=f1.id)


async def test_retention_expiry_sweeper(client, store, monkeypatch):
    owner = await _owner(client, "u2h@example.com")
    monkeypatch.setattr(settings, "DATA_PLANE_RETENTION_MAX_DAYS", 7)
    data = _payload()
    async with get_sessionmaker()() as db:
        _, f = await _upload(db, store, owner, data)
        assert f.expires_at is not None  # the owner's maximum applies even when none was requested
        f.expires_at = utcnow() - dt.timedelta(seconds=1)
        await db.flush()
        with pytest.raises(svc.DataPlaneError):
            await svc.create_download_link(db, owner_id=owner, file_id=f.id)  # gone before the sweeper runs
        out = await svc.sweep_data_plane(db)
        assert out["files_expired"] == 1
        out = await svc.sweep_data_plane(db)
        assert out["objects_deleted"] == 1


async def test_stale_upload_aborted_by_sweeper(client, store):
    owner = await _owner(client, "u2i@example.com")
    async with get_sessionmaker()() as db:
        up, _ = await svc.create_upload(
            db, owner_id=owner, idempotency_key="k9", name="x", content_type="text/plain", sha256=sha(b"a"), size_bytes=1
        )
        up.expires_at = utcnow() - dt.timedelta(seconds=1)
        await db.flush()
        out = await svc.sweep_data_plane(db)
        assert out["uploads_expired"] == 1 and up.status == "expired" and not store.uploads
        with pytest.raises(svc.DataPlaneError):
            await svc.issue_part_url(db, up, part_number=1, sha256=sha(b"a"), size_bytes=1)


async def test_quota_and_size_limits(client, store, monkeypatch):
    owner = await _owner(client, "u2j@example.com")
    monkeypatch.setattr(settings, "DATA_PLANE_ACCOUNT_QUOTA_BYTES", 100)
    async with get_sessionmaker()() as db:
        with pytest.raises(svc.DataPlaneError) as e:
            await svc.create_upload(db, owner_id=owner, idempotency_key="q", name="x", content_type="text/plain",
                                    sha256=sha(b"a"), size_bytes=101)
        assert e.value.code == "quota_exceeded"
    monkeypatch.setattr(settings, "DATA_PLANE_MAX_FILE_BYTES", 10)
    async with get_sessionmaker()() as db:
        with pytest.raises(svc.DataPlaneError) as e:
            await svc.create_upload(db, owner_id=owner, idempotency_key="q2", name="x", content_type="text/plain",
                                    sha256=sha(b"a"), size_bytes=11)
        assert e.value.code == "file_size_not_allowed"


async def test_idempotent_create_and_key_reuse(client, store):
    owner = await _owner(client, "u2k@example.com")
    async with get_sessionmaker()() as db:
        a, _ = await svc.create_upload(db, owner_id=owner, idempotency_key="same", name="x", content_type="text/plain",
                                       sha256=sha(b"a"), size_bytes=1)
        b, _ = await svc.create_upload(db, owner_id=owner, idempotency_key="same", name="x", content_type="text/plain",
                                       sha256=sha(b"a"), size_bytes=1)
        assert a.id == b.id and len(store.uploads) == 1
        with pytest.raises(svc.DataPlaneError) as e:
            await svc.create_upload(db, owner_id=owner, idempotency_key="same", name="x", content_type="text/plain",
                                    sha256=sha(b"b"), size_bytes=1)
        assert e.value.code == "idempotency_key_reused"


async def test_download_link_scoped_clamped_and_owner_only(client, store, monkeypatch):
    owner = await _owner(client, "u2l@example.com")
    other = await _owner(client, "u2m@example.com")
    monkeypatch.setattr(settings, "DATA_PLANE_DOWNLOAD_MAX_TTL_SECONDS", 600)
    async with get_sessionmaker()() as db:
        _, f = await _upload(db, store, owner, _payload())
        link = await svc.create_download_link(db, owner_id=owner, file_id=f.id, ttl_seconds=10**9)
        assert "ttl=600" in link["url"] and link["sha256"] == f.sha256
        assert link["expires_at"] <= utcnow() + dt.timedelta(seconds=601)
        with pytest.raises(svc.DataPlaneError) as e:
            await svc.create_download_link(db, owner_id=other, file_id=f.id)
        assert e.value.http_status == 404


async def test_fails_closed_when_disabled_or_unconfigured(client, store, monkeypatch):
    owner = await _owner(client, "u2n@example.com")
    monkeypatch.setattr(settings, "DATA_PLANE_ENABLED", False)
    async with get_sessionmaker()() as db:
        with pytest.raises(svc.DataPlaneError) as e:
            await svc.create_upload(db, owner_id=owner, idempotency_key="z", name="x", content_type="a/b", sha256=sha(b"a"), size_bytes=1)
        assert e.value.code == "data_plane_disabled"
    r = await client.post("/v1/data/uploads", headers=auth_headers((await register_verify_login(client, "u2o@example.com"))["access_token"]),
                          json={"name": "x", "sha256": sha(b"a"), "size_bytes": 1})
    assert r.status_code in (404, 422)


async def test_http_flow_never_sends_bytes_to_api(client, store):
    tokens = await register_verify_login(client, "u2p@example.com")
    h = {**auth_headers(tokens["access_token"]), "Idempotency-Key": "http1"}
    data = b"hello world"
    r = await client.post("/v1/data/uploads", headers=h, json={"name": "h.txt", "content_type": "text/plain", "sha256": sha(data), "size_bytes": len(data)})
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    r = await client.post(f"/v1/data/uploads/{uid}/parts/1/url", headers=auth_headers(tokens["access_token"]), json={"sha256": sha(data), "size_bytes": len(data)})
    assert r.status_code == 200 and r.json()["url"].startswith("fake://")
    store_uid = next(iter(store.uploads))
    store.client_put(store_uid, 1, data, sha(data))
    assert (await client.post(f"/v1/data/uploads/{uid}/parts/1/confirm", headers=auth_headers(tokens["access_token"]))).status_code == 200
    r = await client.post(f"/v1/data/uploads/{uid}/complete", headers=auth_headers(tokens["access_token"]))
    assert r.status_code == 200 and r.json()["status"] == "complete"
    fid = r.json()["file"]["id"]
    r = await client.post(f"/v1/data/files/{fid}/download-link", headers=auth_headers(tokens["access_token"]), json={})
    assert r.status_code == 200 and r.json()["url"].startswith("fake-get://")


async def test_http_corrupt_part_returns_resend(client, store):
    tokens = await register_verify_login(client, "u2q@example.com")
    h = {**auth_headers(tokens["access_token"]), "Idempotency-Key": "http2"}
    data = b"x" * 100
    r = await client.post("/v1/data/uploads", headers=h, json={"name": "h", "sha256": sha(data), "size_bytes": 100})
    uid = r.json()["id"]
    await client.post(f"/v1/data/uploads/{uid}/parts/1/url", headers=auth_headers(tokens["access_token"]), json={"sha256": sha(data), "size_bytes": 100})
    store.client_put(next(iter(store.uploads)), 1, b"y" * 100, sha(data), enforce=False)
    r = await client.post(f"/v1/data/uploads/{uid}/parts/1/confirm", headers=auth_headers(tokens["access_token"]))
    assert r.status_code == 409 and r.json()["action"] == "resend"
    r = await client.get(f"/v1/data/uploads/{uid}", headers=auth_headers(tokens["access_token"]))
    assert r.json()["corrupt_parts"] == [1]


# ----------------------------- bring your own bucket (U2.4) -----------------------------
class FakeByo(ByoClient):
    """Mirrors an STS session policy: credentials only work under their prefix, and only until expiry."""

    def __init__(self):
        self.now = utcnow()

    def presign(self, *, bucket, credentials, key, method, ttl_seconds):
        return f"https://{bucket.bucket}.example/{key}?m={method}&ttl={ttl_seconds}"

    def scoped_credentials(self, *, bucket, credentials, prefix, read_only, ttl_seconds, session_name):
        return ScopedCredentials("AKIA-T", "sec", "tok", self.now + dt.timedelta(seconds=ttl_seconds), bucket.bucket, prefix, read_only)

    @staticmethod
    def allows(c: ScopedCredentials, key: str, now: dt.datetime) -> bool:
        return now < c.expires_at and key.startswith(c.prefix) and ".." not in key.split("/")


@pytest.fixture
def byo(store, monkeypatch):
    fake = FakeByo()
    set_byo_client(fake)

    async def _job_ok(db, owner_id, job_id):
        return None

    monkeypatch.setattr(svc, "_owned_job", _job_ok)
    monkeypatch.setattr(settings, "DATA_PLANE_BYO_MAX_TTL_SECONDS", 900)
    return fake


async def _register(db, owner, **kw):
    args = dict(owner_id=owner, name="mine", endpoint="", bucket="cust", region="us-east-1", prefix="cc", mode="sts",
                role_arn="arn:aws:iam::1:role/r", access_key_id="AK", secret_access_key="SK")
    args.update(kw)
    return await svc.register_byo_bucket(db, **args)


async def test_u2_4_byo_credentials_scoped_to_job_and_expire(client, byo):
    owner = await _owner(client, "u2r@example.com")
    job1, job2 = uuid.uuid4(), uuid.uuid4()
    async with get_sessionmaker()() as db:
        b = await _register(db, owner)
        assert b.prefix == "cc/" and "SK" not in b.credentials_enc  # stored encrypted, never plain
        c = await svc.issue_byo_scoped_credentials(db, owner_id=owner, bucket_id=b.id, job_id=job1, read_only=True, ttl_seconds=10**6)
        assert c.prefix == f"cc/jobs/{job1}/"
        now = utcnow()
        assert byo.allows(c, f"cc/jobs/{job1}/in/scene.blend", now)
        assert not byo.allows(c, f"cc/jobs/{job2}/in/scene.blend", now)  # another job's path
        assert not byo.allows(c, "cc/other/secret", now) and not byo.allows(c, f"cc/jobs/{job1}/../x", now)
        assert not byo.allows(c, f"cc/jobs/{job1}/a", now + dt.timedelta(seconds=901))  # expired (ttl capped at 900)
        assert c.expires_at <= now + dt.timedelta(seconds=901)


async def test_byo_presigned_url_pinned_to_job_prefix(client, byo):
    owner = await _owner(client, "u2s@example.com")
    job = uuid.uuid4()
    async with get_sessionmaker()() as db:
        b = await _register(db, owner, mode="presign", role_arn=None)
        r = await svc.issue_byo_presigned_url(db, owner_id=owner, bucket_id=b.id, job_id=job, sub_key="out/a.png", method="PUT")
        assert r["key"] == f"cc/jobs/{job}/out/a.png"
        for bad in ("../x", "a/../../x", "/abs", ""):
            with pytest.raises(svc.DataPlaneError):
                await svc.issue_byo_presigned_url(db, owner_id=owner, bucket_id=b.id, job_id=job, sub_key=bad, method="GET")
        with pytest.raises(svc.DataPlaneError):
            await svc.issue_byo_scoped_credentials(db, owner_id=owner, bucket_id=b.id, job_id=job, read_only=True)  # not an sts bucket


async def test_byo_rejects_bad_prefix_endpoint_and_other_owner(client, byo):
    owner = await _owner(client, "u2t@example.com")
    other = await _owner(client, "u2u@example.com")
    async with get_sessionmaker()() as db:
        for prefix in ("", "/", "..", "a/../b"):
            with pytest.raises(svc.DataPlaneError):
                await _register(db, owner, prefix=prefix, name=f"p{abs(hash(prefix))}")
        for ep in ("http://s3.example", "https://169.254.169.254", "https://127.0.0.1", "https://localhost"):
            with pytest.raises(svc.DataPlaneError):
                await _register(db, owner, endpoint=ep, name=f"e{abs(hash(ep))}")
        b = await _register(db, owner)
        with pytest.raises(svc.DataPlaneError) as e:
            await svc.issue_byo_presigned_url(db, owner_id=other, bucket_id=b.id, job_id=uuid.uuid4(), sub_key="a", method="GET")
        assert e.value.http_status == 404
        await svc.revoke_byo_bucket(db, owner_id=owner, bucket_id=b.id)
        with pytest.raises(svc.DataPlaneError):
            await svc.issue_byo_presigned_url(db, owner_id=owner, bucket_id=b.id, job_id=uuid.uuid4(), sub_key="a", method="GET")


async def test_worker_sweep_step_isolated(monkeypatch):
    from app import worker

    calls = []

    async def boom(db):
        calls.append(1)
        raise RuntimeError("storage down")

    monkeypatch.setattr(worker, "sweep_data_plane", boom)
    monkeypatch.setattr(settings, "DATA_PLANE_ENABLED", True)
    for k, v in dict(DATA_PLANE_ENDPOINT="https://x", DATA_PLANE_BUCKET="b", DATA_PLANE_ACCESS_KEY="k", DATA_PLANE_SECRET_KEY_ID="s").items():
        monkeypatch.setattr(settings, k, v)
    timers = {"anchor": float("inf"), "witness": float("inf"), "stake": float("inf")}
    await worker.sweep_once(get_sessionmaker(), timers, 1000.0)  # must not raise
    assert calls == [1] and timers["data_plane"] == 1000.0


async def test_http_complete_keeps_the_corrupt_marker_for_a_part_changed_after_confirm(client, store):
    """A part overwritten in the store after it was confirmed makes complete answer 409 part_changed_resend. The
    corrupt marker must survive that error response (get_db rolls back on an exception) so the resume view tells
    the client which part to send again."""
    tokens = await register_verify_login(client, "u2r@example.com")
    auth = auth_headers(tokens["access_token"])
    data = b"x" * 100
    r = await client.post("/v1/data/uploads", headers={**auth, "Idempotency-Key": "http3"},
                          json={"name": "h", "sha256": sha(data), "size_bytes": 100})
    uid = r.json()["id"]
    await client.post(f"/v1/data/uploads/{uid}/parts/1/url", headers=auth, json={"sha256": sha(data), "size_bytes": 100})
    store_uid = next(iter(store.uploads))
    store.client_put(store_uid, 1, data, sha(data))
    assert (await client.post(f"/v1/data/uploads/{uid}/parts/1/confirm", headers=auth)).status_code == 200
    store.client_put(store_uid, 1, b"y" * 100, sha(data), enforce=False)  # overwritten after it was confirmed
    r = await client.post(f"/v1/data/uploads/{uid}/complete", headers=auth)
    assert r.status_code == 409 and r.json()["detail"] == "part_changed_resend"
    r = await client.get(f"/v1/data/uploads/{uid}", headers=auth)
    assert r.json()["corrupt_parts"] == [1] and r.json()["verified_parts"] == []


async def test_files_list_filters_by_name_prefix_for_job_outputs(client, store):
    """The web console lists a Kit job's outputs as the owner's files named out/<job_id>/<name> (a Kit job has no
    v1 Artifact rows). The filter is owner-scoped and treats % and _ literally."""
    a = await register_verify_login(client, "u2pre-a@example.com")
    b = await register_verify_login(client, "u2pre-b@example.com")
    ua, ub = await get_user_id("u2pre-a@example.com"), await get_user_id("u2pre-b@example.com")
    job = uuid.uuid4()
    async with get_sessionmaker()() as db:
        await _upload(db, store, ua, b"frame-one", name=f"out/{job}/frames", internal_name=True)
        await _upload(db, store, ua, b"other-job", name=f"out/{uuid.uuid4()}/frames", internal_name=True)
        await _upload(db, store, ua, b"plain", name="scene.blend")
        await _upload(db, store, ub, b"bees", name=f"out/{job}/frames", internal_name=True)  # same name, another account
        await db.commit()
    r = await client.get("/v1/data/files", params={"name_prefix": f"out/{job}/"}, headers=auth_headers(a["access_token"]))
    assert r.status_code == 200, r.text
    assert [f["name"] for f in r.json()] == [f"out/{job}/frames"] and r.json()[0]["size_bytes"] == len(b"frame-one")
    r = await client.get("/v1/data/files", params={"name_prefix": "%"}, headers=auth_headers(a["access_token"]))
    assert r.json() == []  # not a wildcard
    r = await client.get("/v1/data/files", headers=auth_headers(a["access_token"]))
    assert len(r.json()) == 3


def test_byo_endpoint_refuses_numeric_and_local_hosts():
    """Numeric spellings of loopback/private addresses (resolver-expanded) and .local names are refused (review-all)."""
    from app.services import data_plane as dp

    for bad in ("https://2130706433/", "https://0x7f.1/", "https://127.1/", "https://10.0.0.1/", "https://[::1]/",
                "https://box.local/", "https://a.localhost/", "http://s3.example.com/", "https://x.internal/"):
        with pytest.raises(dp.DataPlaneError):
            dp._check_endpoint(bad)
    dp._check_endpoint("https://s3.eu-west-1.amazonaws.com")
    dp._check_endpoint("https://storage.example.com:9000/path")
    dp._check_endpoint("")
