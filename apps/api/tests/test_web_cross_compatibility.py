"""Cross-language cryptographic envelope compatibility test (WP-D, item D4).

Verifies that:
1. Sealed envelopes created in Python can be opened in TypeScript (browser runtime).
2. Data envelopes created in Python can be decrypted in TypeScript.
3. Sealed envelopes created in TypeScript can be opened in Python (provider agent / server runtime).
4. Data envelopes created in TypeScript can be decrypted in Python.
5. Shamir shares created in TypeScript can be reconstructed in Python.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.services.privacy_crypto import (
    generate_x25519_keypair,
    seal,
    open_sealed,
    encrypt_data,
    decrypt_data,
    reconstruct_secret,
    canonical_json,
)


def test_cross_language_browser_and_python_envelopes():
    repo_root = Path(__file__).resolve().parents[3]
    web_dir = repo_root / "apps" / "web"

    py_priv, py_pub = generate_x25519_keypair()
    browser_priv, browser_pub = generate_x25519_keypair()

    py_data_key = b"\x42" * 32
    py_sealed_msg = b"Hello from Python server!"
    py_sealed_aad = b"ccpp-v1|cross-lang-test|sealed"
    py_sealed_envelope = seal(browser_pub, py_sealed_msg, py_sealed_aad)

    py_data_msg = b"Data from Python!"
    py_data_aad = b"ccpp-v1|cross-lang-test|data"
    py_data_envelope = encrypt_data(py_data_key, py_data_msg, py_data_aad)

    input_payload = {
        "py_pub": py_pub,
        "browser_priv": browser_priv,
        "browser_pub": browser_pub,
        "py_sealed_envelope": py_sealed_envelope,
        "py_sealed_aad_hex": py_sealed_aad.hex(),
        "expected_py_sealed_msg": py_sealed_msg.decode("utf-8"),
        "py_data_key_hex": py_data_key.hex(),
        "py_data_envelope": py_data_envelope,
        "py_data_aad_hex": py_data_aad.hex(),
        "expected_py_data_msg": py_data_msg.decode("utf-8"),
    }

    # The website's own code, run through the project's vitest (apps/web/src/lib/browser-bridge.test.ts),
    # so nothing is downloaded or cached outside the repository.
    work = Path(tempfile.mkdtemp(prefix="d4-", dir=web_dir))
    try:
        (work / "in.json").write_text(json.dumps({"step": "cross_compat", **input_payload}))
        env = dict(os.environ, CCPP_BRIDGE_INPUT=str(work / "in.json"), CCPP_BRIDGE_OUTPUT=str(work / "out.json"))
        proc = subprocess.run([str(web_dir / "node_modules/.bin/vitest"), "run", "src/lib/browser-bridge.test.ts"],
                              cwd=str(web_dir), env=env, capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
        ts_result = json.loads((work / "out.json").read_text())
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # 1. Python opens browser's sealed envelope
    browser_sealed_aad = bytes.fromhex(ts_result["browserSealedAadHex"])
    py_opened_sealed = open_sealed(py_priv, ts_result["browserSealedEnv"], browser_sealed_aad)
    assert py_opened_sealed == b"Hello from Browser TypeScript!"

    # 2. Python decrypts browser's data envelope
    browser_data_key = bytes.fromhex(ts_result["browserDataKeyHex"])
    browser_data_aad = bytes.fromhex(ts_result["browserDataAadHex"])
    py_opened_data = decrypt_data(browser_data_key, ts_result["browserDataEnv"], browser_data_aad)
    assert py_opened_data == b"Data from Browser TypeScript!"

    # 3. Python reconstructs Shamir secret created in browser
    shares = [(s["x"], bytes.fromhex(s["shareHex"])) for s in ts_result["serializableShares"][:2]]
    reconstructed = reconstruct_secret(shares, k=2)
    assert reconstructed == b"\x99" * 32
