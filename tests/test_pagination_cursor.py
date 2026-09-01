"""Cursor encoding/decoding unit tests."""

import base64
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.domain.pagination import (
    InvalidCursorError,
    decode_issue_cursor,
    encode_issue_cursor,
)


def encode_payload(payload) -> str:
    """Build a cursor from an arbitrary payload, bypassing the encoder."""
    raw = json.dumps(payload).encode("utf-8")

    return base64.urlsafe_b64encode(raw).decode("ascii")


def test_round_trip_preserves_values():
    created_at = datetime(2026, 8, 31, 13, 52, 18, 17718, tzinfo=timezone.utc)
    issue_id = uuid4()

    cursor = encode_issue_cursor(created_at, issue_id)
    decoded = decode_issue_cursor(cursor)

    assert decoded.created_at == created_at
    assert decoded.id == issue_id


def test_encoding_is_deterministic():
    created_at = datetime(2026, 8, 31, 13, 52, 18, 17718, tzinfo=timezone.utc)
    issue_id = UUID("01a05817-9e91-7efe-a65b-490730cfa392")

    assert encode_issue_cursor(created_at, issue_id) == encode_issue_cursor(
        created_at, issue_id
    )


def test_cursor_is_url_safe():
    created_at = datetime(2026, 8, 31, 13, 52, 18, 17718, tzinfo=timezone.utc)

    cursor = encode_issue_cursor(created_at, uuid4())

    assert "+" not in cursor
    assert "/" not in cursor


def test_encoding_a_naive_datetime_is_a_programming_error():
    with pytest.raises(ValueError):
        encode_issue_cursor(datetime(2026, 1, 1), uuid4())


def test_non_utc_timezone_round_trips():
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc).astimezone()

    decoded = decode_issue_cursor(encode_issue_cursor(created_at, uuid4()))

    assert decoded.created_at == created_at


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64!!!",
        base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode("ascii"),
        base64.urlsafe_b64encode(b"not json at all").decode("ascii"),
        encode_payload(["not", "a", "dict"]),
        encode_payload({"id": "01a05817-9e91-7efe-a65b-490730cfa392"}),
        encode_payload({"created_at": "2026-01-01T00:00:00+00:00"}),
        encode_payload({"created_at": "2026-01-01T00:00:00+00:00", "id": "nope"}),
        encode_payload({"created_at": "yesterday", "id": str(UUID(int=1))}),
        encode_payload({"created_at": "2026-01-01T00:00:00", "id": str(UUID(int=1))}),
        encode_payload({"created_at": 12345, "id": str(UUID(int=1))}),
        encode_payload({"created_at": None, "id": None}),
        "",
    ],
)
def test_malformed_cursors_raise_invalid_cursor_error(cursor):
    with pytest.raises(InvalidCursorError) as exc_info:
        decode_issue_cursor(cursor)

    # Generic message only -- no parser detail reaches the client.
    assert str(exc_info.value) == "Invalid cursor"


def test_invalid_cursor_error_does_not_chain_parser_exceptions():
    """The raw decoding exception must not ride along as __cause__."""
    with pytest.raises(InvalidCursorError) as exc_info:
        decode_issue_cursor("not-base64!!!")

    assert exc_info.value.__cause__ is None
