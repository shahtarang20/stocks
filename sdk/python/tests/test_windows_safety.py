"""W9 (cross-platform plan): Windows-safety checks for the Python SDK. WRITTEN, NOT RUN.

Every test here is meant to pass on Linux AND on Windows (``windows-latest`` in .github/workflows/ci.yml). They pin:
UTF-8 and canonical JSON bytes that never depend on the OS, binary-safe handling, files saved by Windows PowerShell,
console code pages, path handling, and that the SDK source has no POSIX-only call.

Bug re-insertion list: read the receipt with ``open(path, encoding="utf-8")`` again (the BOM and UTF-16 tests fail);
remove the stdout ``reconfigure`` in ccverify.main (the cp1252 console test fails); add ``import fcntl`` to the SDK (the
source scan fails); make ``canonical_json`` use ``ensure_ascii=False`` (the ASCII-only test fails).
"""

from __future__ import annotations

import codecs
import io
import json
import pathlib
import re
import sys

import pytest

from computecoin import ccverify
from computecoin.crypto import build_private_chat_input
from computecoin.private_kit import build_private_kit_input
from computecoin.privacy_crypto import canonical_json
from computecoin.private_stream import iter_sse_data

SDK_SRC = pathlib.Path(ccverify.__file__).resolve().parent


# --------------------------------------------------------------------------- canonical JSON: same bytes on every OS
def test_canonical_json_escapes_line_endings_and_is_ascii_only():
    out = canonical_json({"b": 1, "a": "x\r\ny"})
    assert out == b'{"a":"x\\r\\ny","b":1}'
    assert b"\r" not in out and b"\n" not in out
    assert not out.endswith(b"\n")


def test_canonical_json_non_ascii_and_del_are_escaped_lower_case():
    # Expected strings come from CPython's json.dumps(..., sort_keys=True, separators=(",", ":")); the TypeScript SDK
    # asserts the same strings in sdk/typescript/src/windows-safety.test.ts.
    assert canonical_json({"k": "\x7f"}) == b'{"k":"\\u007f"}'
    assert canonical_json({"k": "caf\u00e9 \U0001F600"}) == b'{"k":"caf\\u00e9 \\ud83d\\ude00"}'
    assert canonical_json({"k": "\u2028"}) == b'{"k":"\\u2028"}'
    assert canonical_json({"k": 'a\tb\\c"'}) == b'{"k":"a\\tb\\\\c\\""}'
    sample = canonical_json({"name": "\u00fc\u00f1\u00ee", "text": "l1\r\nl2\n", "e": "\U0001F680"})
    assert all(0x20 <= b <= 0x7E for b in sample)


def test_chat_input_keeps_crlf_inside_the_message_and_the_prompt():
    built = build_private_chat_input([{"role": "user", "content": "a\r\nb"}])
    assert built["messages"][0]["content"] == "a\r\nb"
    assert built["prompt"] == "user: a\r\nb"
    assert json.loads(canonical_json(built).decode("ascii")) == built


def test_private_kit_binary_input_is_bit_exact_and_text_keeps_crlf():
    import base64

    raw = b"\r\n\x00\x1a\xff"
    built = build_private_kit_input({"data.bin": raw, "note.txt": "a\r\nb\r\n"})
    assert base64.b64decode(built["inputs"]["data.bin"]["b64"]) == raw
    assert built["inputs"]["note.txt"]["text"] == "a\r\nb\r\n"


@pytest.mark.parametrize("bad", ["a\\b", "a/b", "C:\\x", "a b", "", "con\x00", "a\n", "a\r\n"])
def test_private_kit_input_names_are_never_paths(bad):
    with pytest.raises(ValueError):
        build_private_kit_input({bad: "x"})


def test_sse_lines_with_crlf_and_bom_parse():
    lines = ['data: {"n": 1}\r\n', "\r\n", 'data: {"n": 2}\r\n', "data: [DONE]\r\n"]
    assert list(iter_sse_data(lines)) == [{"n": 1}, {"n": 2}]


# --------------------------------------------------------------------------- reading files that Windows tools wrote
RECEIPT = {"isolation_tier": "sealed", "machine_id": "m-\u00e9", "note": "line1\r\nline2"}


@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-8-sig", "utf-16"],  # utf-16 writes a BOM + little endian: what Windows PowerShell 5.1 `>` produces
)
def test_read_json_file_accepts_utf8_bom_and_utf16(tmp_path, encoding):
    p = tmp_path / "receipt.json"
    p.write_bytes(json.dumps(RECEIPT, indent=2, ensure_ascii=False).replace("\n", "\r\n").encode(encoding))
    assert ccverify.read_json_file(str(p)) == RECEIPT


def test_read_json_file_utf16_big_endian_with_bom(tmp_path):
    p = tmp_path / "r.json"
    p.write_bytes(codecs.BOM_UTF16_BE + json.dumps(RECEIPT).encode("utf-16-be"))
    assert ccverify.read_json_file(str(p)) == RECEIPT


def test_read_json_file_rejects_garbage_with_a_value_error(tmp_path):
    p = tmp_path / "r.json"
    p.write_bytes(b"\x80\x81\x82 not json")
    with pytest.raises(ValueError):
        ccverify.read_json_file(str(p))


def _stub_verify(monkeypatch, machine="m-1"):
    def fake(receipt, *a, **k):
        return {"verified": True, "isolation_tier": receipt.get("isolation_tier"), "machine_id": machine, "checks": {"verdict_signature": True}}

    monkeypatch.setattr(ccverify, "verify_receipt", fake)


def test_cli_reads_a_powershell_utf16_receipt(tmp_path, monkeypatch, capsys):
    _stub_verify(monkeypatch)
    p = tmp_path / "receipt.json"
    p.write_bytes(json.dumps({"isolation_tier": "sealed"}).encode("utf-16"))
    assert ccverify.main([str(p), "--verifier-key", "00" * 32]) == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_cli_missing_receipt_is_a_clean_error_not_a_traceback(tmp_path, capsys):
    assert ccverify.main([str(tmp_path / "nope.json"), "--verifier-key", "00" * 32]) == 2
    assert "cannot read receipt" in capsys.readouterr().err


def test_cli_witness_key_file_given_as_a_native_path(tmp_path, monkeypatch):
    """A Windows path has a drive colon and backslashes; a POSIX one has slashes. Neither may be mistaken for ``name=hex``."""
    _stub_verify(monkeypatch)
    receipt = tmp_path / "r.json"
    receipt.write_text(json.dumps({"isolation_tier": "sealed"}), encoding="utf-8")
    keys = tmp_path / "w.json"
    keys.write_text(json.dumps({"w1": "AB" * 32}), encoding="utf-8")
    assert ccverify.main([str(receipt), "--verifier-key", "00" * 32, "--witness-keys", str(keys)]) == 0
    missing = str(tmp_path / "missing-witnesses.json")  # not a file, no "=" -> refused, exit 2, no crash
    assert ccverify.main([str(receipt), "--verifier-key", "00" * 32, "--witness-keys", missing]) == 2


def test_cli_survives_a_console_that_cannot_encode_the_output(tmp_path, monkeypatch):
    """Windows consoles on a legacy code page (cp1252/cp437) raise UnicodeEncodeError on print() of other characters."""
    _stub_verify(monkeypatch, machine="host-\u2603")
    receipt = tmp_path / "r.json"
    receipt.write_text(json.dumps({"isolation_tier": "sealed"}), encoding="utf-8")
    raw = io.BytesIO()
    fake_out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\r\n", write_through=True)
    monkeypatch.setattr(sys, "stdout", fake_out)
    assert ccverify.main([str(receipt), "--verifier-key", "00" * 32]) == 0
    fake_out.flush()
    text = raw.getvalue().decode("cp1252")
    assert "host-\\u2603" in text and "VERIFIED" in text


# --------------------------------------------------------------------------- the SDK source has no POSIX-only call
FORBIDDEN = [
    r"\bfcntl\b", r"\bos\.getuid\b", r"\bos\.geteuid\b", r"\bos\.getgid\b", r"\bos\.mkfifo\b", r"\bos\.fork\b",
    r"\bos\.setsid\b", r"\bsignal\.SIGKILL\b", r"\bsignal\.SIGALRM\b", r"\balarm\(", r"add_signal_handler",
    r"/proc/", r"/dev/", r"/sys/", r"/tmp\b", r"\bos\.chmod\b", r"\bimport pwd\b", r"\bimport grp\b",
    r"\bos\.symlink\b", r"NamedTemporaryFile\(", r"\bimport asyncio\b", r"\bshell=True\b",
]


def _source_files():
    return sorted(p for p in SDK_SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_sdk_source_has_no_posix_only_call():
    hits = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            for m in re.finditer(pattern, text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{path.name}:{line}: {m.group(0)}")
    assert hits == []


def test_sdk_source_never_opens_text_files_with_the_locale_encoding():
    """open()/read_text()/write_text() in text mode must name an encoding (the Windows default is a legacy code page)."""
    bad = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\bopen\(([^)]*)\)|\.(?:read_text|write_text)\(([^)]*)\)", text):
            args = m.group(1) if m.group(1) is not None else m.group(2)
            binary = re.search(r"""["'][rwa+]*b[rwa+]*["']""", args or "")
            if "encoding=" not in (args or "") and not binary:
                bad.append(f"{path.name}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}")
    assert bad == []


def test_sdk_source_is_utf8_with_lf_line_endings():
    """A checkout with autocrlf would turn these into CRLF; the workflow sets core.autocrlf=false and .gitattributes pins LF."""
    for path in _source_files():
        raw = path.read_bytes()
        raw.decode("utf-8")
        assert b"\r\n" not in raw, f"{path.name} has CRLF line endings"


def test_windows_event_loop_note():
    """The SDK is synchronous (httpx.Client), so it never creates an event loop. If an async client is added, it must
    work on the default Proactor loop of Windows (no add_signal_handler, no selector-only APIs); this test then moves
    to a real loop check. Today it only pins that nothing in the package imports asyncio."""
    for path in _source_files():
        assert "import asyncio" not in path.read_text(encoding="utf-8"), path.name
