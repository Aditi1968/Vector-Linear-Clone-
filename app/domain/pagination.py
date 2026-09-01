import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.issues import IssueEntity


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
class IssuePage:
    nodes: list[IssueEntity]
    has_next_page: bool
    end_cursor: str | None


def encode_issue_cursor(created_at: datetime, issue_id: UUID) -> str:
    """Encode the keyset position as an opaque URL-safe Base64 string.

    A naive datetime here is a programming error, not user input, so it
    raises ValueError rather than InvalidCursorError.
    """
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")

    payload = {
        "created_at": created_at.isoformat(),
        "id": str(issue_id),
    }

    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    return base64.urlsafe_b64encode(encoded.encode("utf-8")).decode("ascii")


def decode_issue_cursor(cursor: str) -> IssueCursor:
    """Decode an opaque cursor, or raise InvalidCursorError.

    Every parsing failure collapses into InvalidCursorError so that no
    base64/JSON/UUID/datetime detail can escape to a client.
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

    raw_created_at = payload.get("created_at")
    raw_id = payload.get("id")

    if not isinstance(raw_created_at, str) or not isinstance(raw_id, str):
        raise InvalidCursorError()

    try:
        created_at = datetime.fromisoformat(raw_created_at)
        issue_id = UUID(raw_id)
    except (AttributeError, TypeError, ValueError):
        raise InvalidCursorError() from None

    if created_at.tzinfo is None:
        raise InvalidCursorError()

    return IssueCursor(created_at=created_at, id=issue_id)
