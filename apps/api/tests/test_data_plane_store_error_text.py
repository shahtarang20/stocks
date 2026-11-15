"""The data plane's log line says what the object store answered, in the main message (log viewers cut long fields)."""

from __future__ import annotations

from botocore.exceptions import ClientError

from app.services import data_plane as dp


def _client_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"RequestId": "r1"}}, "CreateMultipartUpload")


def test_a_store_error_is_summarised_as_code_and_message():
    text = dp._store_error_text(_client_error("InvalidArgument", "Header 'x-amz-checksum-algorithm'\nis not supported"))
    assert text == "InvalidArgument: Header 'x-amz-checksum-algorithm' is not supported"


def test_other_errors_fall_back_to_their_type_and_the_text_is_capped():
    assert dp._store_error_text(RuntimeError("boom")) == "RuntimeError: boom"
    assert dp._store_error_text(ValueError()) == "ValueError: "
    assert len(dp._store_error_text(_client_error("X", "y" * 5000))) == 400


def test_the_summary_never_raises_even_for_an_odd_exception():
    class Odd(Exception):
        response = "not a dict"

    assert dp._store_error_text(Odd("x")) == "Odd"

