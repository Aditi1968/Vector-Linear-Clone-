import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.comments import CommentEntity
from app.domain.issues import IssueEntity
from app.domain.labels import LabelEntity


class InvalidCursorError(Exception):
    """An opaque cursor could not be decoded or validated.

    The message is deliberately generic: cursors are opaque to clients, so
    parser details are never part of the contract.
    """

    def __init__(self):
        super().__init__("Invalid cursor")


@dataclass(frozen=True, slots=True)
class IssueCursor:
    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class LabelCursor:
    """A position in a workspace's labels, ordered by name.

    A different key from IssueCursor because labels are read alphabetically,
    not newest-first, and a cursor is only meaningful against the ordering it
    was minted for. `name` is the stored value verbatim -- never folded --
    so the server compares it under the same collation that produced the
    ordering.
    """

    name: str
    id: UUID


@dataclass(frozen=True, slots=True)
class IssuePage:
    nodes: list[IssueEntity]
    has_next_page: bool
    end_cursor: str | None


@dataclass(frozen=True, slots=True)
class LabelPage:
    nodes: list[LabelEntity]
    has_next_page: bool
    end_cursor: str | None


@dataclass(frozen=True, slots=True)
class CommentPage:
    nodes: list[CommentEntity]
    has_next_page: bool
    end_cursor: str | None


def _encode_payload(payload: dict[str, str]) -> str:
    """Render a cursor payload as an opaque URL-safe Base64 string.

    Shared by every codec below so that all cursors are one wire format. The
    JSON is written with fixed separators and sorted keys, which is what makes
    encoding deterministic: the same position always yields the same string,
    so a client comparing two cursors for equality gets the answer it expects.
    """
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    return base64.urlsafe_b64encode(encoded.encode("utf-8")).decode("ascii")


def _decode_payload(cursor: str) -> dict:
    """The object inside a cursor, or InvalidCursorError.

    Every parsing failure collapses into InvalidCursorError so that no
    base64/JSON detail can escape to a client. A payload that decodes to
    something other than an object -- a bare number, a list, `null` -- is as
    invalid as one that does not decode at all.
    """
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (
        AttributeError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
        UnicodeEncodeError,
        ValueError,
    ):
        raise InvalidCursorError() from None

    if not isinstance(payload, dict):
        raise InvalidCursorError()

    return payload


def _string_field(payload: dict, key: str) -> str:
    """One required string out of a decoded payload.

    A cursor is client-supplied text, so a missing key and a key holding a
    number are both ordinary invalid input rather than programming errors.
    """
    value = payload.get(key)

    if not isinstance(value, str):
        raise InvalidCursorError()

    return value


def encode_issue_cursor(created_at: datetime, issue_id: UUID) -> str:
    """Encode the keyset position as an opaque URL-safe Base64 string.

    A naive datetime here is a programming error, not user input, so it
    raises ValueError rather than InvalidCursorError.
    """
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")

    return _encode_payload(
        {
            "created_at": created_at.isoformat(),
            "id": str(issue_id),
        }
    )


def decode_issue_cursor(cursor: str) -> IssueCursor:
    """Decode an opaque cursor, or raise InvalidCursorError.

    Every parsing failure collapses into InvalidCursorError so that no
    base64/JSON/UUID/datetime detail can escape to a client.
    """
    payload = _decode_payload(cursor)

    raw_created_at = _string_field(payload, "created_at")
    raw_id = _string_field(payload, "id")

    try:
        created_at = datetime.fromisoformat(raw_created_at)
        issue_id = UUID(raw_id)
    except (AttributeError, TypeError, ValueError):
        raise InvalidCursorError() from None

    if created_at.tzinfo is None:
        raise InvalidCursorError()

    return IssueCursor(created_at=created_at, id=issue_id)


def encode_label_cursor(name: str, label_id: UUID) -> str:
    """Encode a position in the alphabetical label ordering.

    The name is carried exactly as stored. Folding it here would mint a
    cursor the server cannot compare against the column it ordered by, and
    Python's idea of lowercase is not PostgreSQL's on every string.
    """
    return _encode_payload({"name": name, "id": str(label_id)})


def decode_label_cursor(cursor: str) -> LabelCursor:
    """Decode a label cursor, or raise InvalidCursorError.

    The name is not validated beyond being a string: any text is a legal
    position in an ordering, and a name that matches no label simply resumes
    where that name would have sorted. There is nothing to leak by accepting
    it, and rejecting unknown names would turn the cursor into an oracle for
    which labels exist.
    """
    payload = _decode_payload(cursor)

    name = _string_field(payload, "name")
    raw_id = _string_field(payload, "id")

    try:
        label_id = UUID(raw_id)
    except (AttributeError, TypeError, ValueError):
        raise InvalidCursorError() from None

    return LabelCursor(name=name, id=label_id)
