"""Private stream un-padding, sealed stream reader, private Kit input and private fan-out (offline, not run here)."""

import base64
import json
import pathlib

import httpx
import pytest

from computecoin import ComputeCoinClient, build_private_kit_input
from computecoin.crypto import generate_customer_keypair
from computecoin.private_kit import prepare_private_fanout_parts
from computecoin.private_stream import (
    PrivateStreamError,
    PrivateStreamReader,
    iter_private_text,
    open_stream_chunk,
    stream_aad,
    unpad_stream_plaintext,
)
from computecoin.privacy_crypto import (
    INPUT_AAD,
    decrypt_data,
    generate_x25519_keypair,
    open_sealed,
    reconstruct_secret,
    seal,
    share_aad,
)

JOB = "00000000-0000-4000-8000-0000000000a0"


def pad(data: bytes, block: int) -> bytes:  # same framing as provider-agent pad_stream_plaintext
    if block <= 0:
        return data
    framed = len(data).to_bytes(4, "big") + data
    return framed + b"\x00" * (-len(framed) % block)


def sealed_event(pub, text: str, seq: int, final: bool, block: int = 256, job: str = JOB, plain: bytes | None = None):
    body = plain if plain is not None else pad(text.encode(), block)
    return {"object": "chat.completion.chunk.sealed", "job_id": job, "seq": seq, "final": final,
            "sealed": seal(pub, body, stream_aad(job, seq, final))}


def sse(events):
    return [f"data: {json.dumps(e)}" for e in events] + ["data: [DONE]"]


def test_unpad_roundtrip_and_rejections():
    assert unpad_stream_plaintext(pad(b"hello", 256)) == b"hello"
    assert unpad_stream_plaintext(pad(b"", 256)) == b""
    assert len(pad(b"x" * 300, 256)) == 512
    with pytest.raises(PrivateStreamError):
        unpad_stream_plaintext(b"\x00\x00")  # shorter than prefix
    with pytest.raises(PrivateStreamError):
        unpad_stream_plaintext((10).to_bytes(4, "big") + b"abc")  # length exceeds payload
    bad = bytearray(pad(b"hi", 16))
    bad[-1] = 1
    with pytest.raises(PrivateStreamError):
        unpad_stream_plaintext(bytes(bad))  # non-zero padding


def test_spec_vector_when_populated():
    root = pathlib.Path(__file__).resolve().parents[3]
    sec = json.loads((root / "docs/specs/ccpp-v1-vectors.json").read_text(encoding="utf-8"))["fanout_v1"]
    if not sec.get("inputs") or not sec.get("expected"):
        pytest.skip("fanout_v1 vectors not generated yet; round-trip tests below cover the format")
    i, chunk = sec["inputs"]["stream"], sec["expected"]["stream_chunk"]
    aad = stream_aad(i["job_id"], i["seq"], i["final"])
    assert aad.decode() == chunk["aad_utf8"]
    padded = open_sealed(i["recipient_private_hex"], chunk["envelope"], aad)
    assert padded.hex() == chunk["padded_plaintext_hex"]
    assert len(padded) == chunk["padded_length"]
    assert unpad_stream_plaintext(padded).decode() == i["text_utf8"]
    assert open_stream_chunk(i["recipient_private_hex"], chunk["envelope"], job_id=i["job_id"], seq=i["seq"],
                             final=i["final"], pad_bytes=i["pad_bytes"]) == i["text_utf8"]


def test_reader_in_order_final_and_unicode():
    priv, pub = generate_x25519_keypair()
    evs = [sealed_event(pub, "Hel", 0, False), sealed_event(pub, "lo é世", 1, False), sealed_event(pub, "!", 2, True)]
    assert "".join(iter_private_text(sse(evs), priv)) == "Hello é世!"


def test_reader_unpadded_stream():
    priv, pub = generate_x25519_keypair()
    evs = [sealed_event(pub, "ab", 0, False, block=0), sealed_event(pub, "c", 1, True, block=0)]
    assert "".join(iter_private_text(sse(evs), priv, pad_bytes=0)) == "abc"


def test_reader_rejects_reorder_replay_truncation_and_other_job():
    priv, pub = generate_x25519_keypair()
    e0, e1, e2 = (sealed_event(pub, "a", 0, False), sealed_event(pub, "b", 1, False), sealed_event(pub, "c", 2, True))
    with pytest.raises(PrivateStreamError):
        list(iter_private_text(sse([e1, e0, e2]), priv))
    with pytest.raises(PrivateStreamError):
        list(iter_private_text(sse([e0, e0]), priv))
    with pytest.raises(PrivateStreamError, match="truncated"):
        list(iter_private_text(sse([e0, e1]), priv))
    other = sealed_event(pub, "b", 1, False, job="00000000-0000-4000-8000-0000000000b1")
    with pytest.raises(PrivateStreamError):
        list(iter_private_text(sse([e0, other]), priv))
    # a flipped final flag changes the AAD, so the chunk does not open
    flipped = dict(e2, final=False)
    with pytest.raises(PrivateStreamError):
        list(iter_private_text(sse([e0, e1, flipped]), priv))


def test_reader_rejects_wrong_key_bad_padding_and_plaintext_event():
    priv, pub = generate_x25519_keypair()
    other_priv, _ = generate_x25519_keypair()
    ev = sealed_event(pub, "x", 0, True)
    with pytest.raises(PrivateStreamError):
        PrivateStreamReader(other_priv).feed(ev)
    tampered = sealed_event(pub, "", 0, True, plain=(2).to_bytes(4, "big") + b"hi" + b"\x00" * 9 + b"\x01")
    with pytest.raises(PrivateStreamError):
        PrivateStreamReader(priv).feed(tampered)
    with pytest.raises(PrivateStreamError):
        PrivateStreamReader(priv).feed({"object": "chat.completion.chunk", "choices": []})
    with pytest.raises(PrivateStreamError, match="stream error"):
        PrivateStreamReader(priv).feed({"error": {"code": "sealed_stream_incomplete"}})


def test_client_private_stream_end_to_end_with_mock_transport():
    captured = {}
    keys = {"guardians": {"g1": generate_x25519_keypair()[1], "g2": generate_x25519_keypair()[1], "g3": generate_x25519_keypair()[1]}, "threshold": 2}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/privacy/verifier-key":
            return httpx.Response(200, json=keys)
        body = json.loads(request.content)
        captured["body"] = body
        pub = body["result_public_key"]
        evs = [sealed_event(pub, "Hi ", 0, False), sealed_event(pub, "there", 1, True)]
        return httpx.Response(200, text="".join(l + "\n\n" for l in sse(evs)), headers={"content-type": "text/event-stream"})

    client = ComputeCoinClient("https://api.test", transport=httpx.MockTransport(handler), access_token="t")
    with pytest.warns(UserWarning):
        out = "".join(client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "secret question"}], stream=True, isolation_tier="sealed"))
    assert out == "Hi there"
    body = captured["body"]
    assert body["messages"] == [{"role": "user", "content": "sealed"}]
    assert "secret question" not in json.dumps(body)
    assert body["isolation_tier"] == "sealed" and body["stream"] is True


def test_client_private_stream_flag_off_raises_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/privacy/verifier-key":
            return httpx.Response(200, json={"guardians": {"a": generate_x25519_keypair()[1], "b": generate_x25519_keypair()[1]}, "threshold": 2})
        return httpx.Response(400, json={"detail": {"error": {"code": "streaming_not_supported_for_private"}}})

    client = ComputeCoinClient("https://api.test", transport=httpx.MockTransport(handler), access_token="t")
    with pytest.warns(UserWarning), pytest.raises(Exception, match="streaming_not_supported_for_private"):
        list(client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}], stream=True, isolation_tier="sealed"))


def test_plain_stream_unchanged_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "encrypted_input" not in json.loads(request.content)
        chunks = [{"choices": [{"delta": {"content": "a"}}]}, {"choices": [{"delta": {"content": "b"}}]}]
        return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")

    client = ComputeCoinClient("https://api.test", transport=httpx.MockTransport(handler), access_token="t")
    assert list(client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}], stream=True)) == ["a", "b"]


# ------------------------------------------------------------------ private Kit input / fan-out
def test_build_private_kit_input_shapes():
    out = build_private_kit_input({"img": b"\x00\x01", "note": "hi", "pre": {"b64": "AAE="}, "t": {"text": "x"}}, {"n": 1})
    assert out == {"inputs": {"img": {"b64": "AAE="}, "note": {"text": "hi"}, "pre": {"b64": "AAE="}, "t": {"text": "x"}}, "params": {"n": 1}}
    assert build_private_kit_input() == {"inputs": {}, "params": {}}
    for bad in ({"../x": "a"}, {"a/b": "a"}, {"": "a"}, {"ok": 5}, {"ok": {"b64": "!!"}}, {"ok": {"b64": "AAE=", "text": "x"}}):
        with pytest.raises(ValueError):
            build_private_kit_input(bad)


def test_prepare_private_fanout_parts_decrypt_and_distinct_keys():
    gpriv = {n: generate_x25519_keypair() for n in ("g1", "g2", "g3")}
    guardians = {n: pub for n, (_, pub) in gpriv.items()}
    plains = [build_private_kit_input({"f": b"one"}, {"i": 0}), build_private_kit_input({"f": b"two"}, {"i": 1})]
    parts, privs = prepare_private_fanout_parts(plains, guardians=guardians, threshold=2)
    assert len(parts) == 2 and len(set(privs)) == 2
    assert len({p["result_public_key"] for p in parts}) == 2
    for p, plain in zip(parts, plains):
        assert set(p) == {"encrypted_input", "dk_shares", "result_public_key"}
        shares = []
        for name in ("g1", "g2"):
            sh = p["dk_shares"][name]
            shares.append((sh["x"], open_sealed(gpriv[name][0], sh["envelope"], share_aad(name))))
        dk = reconstruct_secret(shares, 2)
        assert json.loads(decrypt_data(dk, p["encrypted_input"], INPUT_AAD)) == plain
    with pytest.raises(ValueError):
        prepare_private_fanout_parts([{"inputs": {}, "params": {}, "extra": 1}], guardians=guardians, threshold=2)


def test_submit_private_fanout_posts_private_parts():
    guardians = {n: generate_x25519_keypair()[1] for n in ("g1", "g2", "g3")}
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/privacy/verifier-key":
            return httpx.Response(200, json={"guardians": guardians, "threshold": 2})
        seen["path"], seen["body"], seen["idem"] = request.url.path, json.loads(request.content), request.headers.get("Idempotency-Key")
        return httpx.Response(201, json={"parent_job_id": "p", "part_count": 2})

    client = ComputeCoinClient("https://api.test", transport=httpx.MockTransport(handler), access_token="t")
    with pytest.warns(UserWarning):
        sub = client.submit_private_fanout(
            spec={"kit": "k"}, parts=[build_private_kit_input({"a": "1"}), build_private_kit_input({"a": "2"})],
            isolation_tier="sealed", idempotency_key="idem-1")
    assert seen["path"] == "/v1/jobs/v2/fanout" and seen["idem"] == "idem-1"
    assert seen["body"]["isolation_tier"] == "sealed" and len(seen["body"]["private_parts"]) == 2
    assert [p["result_public_key"] for p in seen["body"]["private_parts"]] != []
    assert len(sub.result_private_keys) == 2 and sub.fanout["part_count"] == 2
    with pytest.raises(ValueError):
        client.submit_private_fanout(spec={}, parts=[build_private_kit_input()], isolation_tier="container")


# --- private chat input contract (shared vectors with the TypeScript SDK; written, not run) ---
def _chat_vectors():
    root = pathlib.Path(__file__).resolve().parents[3]
    return json.loads((root / "docs/specs/private-chat-input-vectors.json").read_text(encoding="utf-8"))["cases"]


def test_private_chat_input_matches_shared_canonical_vectors():
    from computecoin import build_private_chat_input
    from computecoin.privacy_crypto import canonical_json

    for case in _chat_vectors():
        built = build_private_chat_input(case["messages"], max_tokens=case.get("max_tokens"), temperature=case.get("temperature"))
        assert canonical_json(built).decode("utf-8") == case["expected"], case["name"]
        assert set(built) <= {"prompt", "messages", "max_tokens", "temperature"}


def test_private_chat_input_validation():
    from computecoin import build_private_chat_input

    with pytest.raises(ValueError):
        build_private_chat_input([])
    with pytest.raises(ValueError):
        build_private_chat_input([{"role": "user", "content": "x"}], max_tokens=0)
    with pytest.raises(ValueError):
        build_private_chat_input([{"role": "user", "content": "x"}], temperature=3)


def test_private_chat_job_encrypts_canonical_bytes():
    from computecoin import build_private_chat_input
    from computecoin.crypto import encrypt_job_input
    from computecoin.privacy_crypto import canonical_json, decrypt_data

    plain = build_private_chat_input([{"role": "user", "content": "hi"}], max_tokens=8)
    env, key = encrypt_job_input(plain, canonical=True)
    assert decrypt_data(key, env, INPUT_AAD) == canonical_json(plain)
