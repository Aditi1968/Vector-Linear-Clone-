import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.domain.comments import CommentEntity
from app.domain.issues import (
    IssueEntity,
    IssueOrder,
    IssueOrderField,
    OrderDirection,
    OrderKey,
)
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
class IssueListCursor:
    """A position in one ordering of the issue list.

    `key` is whatever that ordering sorts by -- a timestamp, a date, a small
    integer -- and None where the row has no key at all: an issue with no due
    date, or one nobody has given a priority. That is a position like any
    other, not a missing value, and the keyset predicate has a branch for it.

    `order` is carried so a cursor cannot be replayed under a different sort.
    IssueCursor above cannot express any of this and stays as it is: three
    other lists page by `(created_at, id)` and none of them sorts by anything
    else.
    """

    order: IssueOrder
    key: OrderKey
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


def _encode_payload(payload: dict[str, str | None]) -> str:
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


def _optional_string_field(payload: dict, key: str) -> str | None:
    """One string out of a decoded payload, where JSON null is a real value.

    Separate from `_string_field` rather than a flag on it: a null `k` means
    the row this cursor names sorts with no key at all -- no due date, no
    priority -- which is a position in the ordering and not a missing field.
    A key that is absent entirely is still invalid.
    """
    if key not in payload:
        raise InvalidCursorError()

    value = payload[key]

    if value is not None and not isinstance(value, str):
        raise InvalidCursorError()

    return value


def _order_key_text(key: OrderKey) -> str | None:
    """A sort key as text, in the one spelling `_parse_order_key` reads back.

    `datetime` before `date`, because a datetime IS a date and the wrong
    branch would truncate a timestamp to its day -- which resumes a walk at
    midnight and hands back a page the client has already seen.
    """
    if key is None:
        return None

    if isinstance(key, datetime | date):
        return key.isoformat()

    return str(key)


def _parse_order_key(field: IssueOrderField, text: str | None) -> OrderKey:
    """Text back into the type the column compares as, or InvalidCursorError.

    The type comes from the FIELD the cursor names, never from the shape of
    the text, so a cursor cannot smuggle a datetime into a comparison against
    `priority` and make the server raise from the driver.
    """
    if text is None:
        return None

    try:
        if field is IssueOrderField.PRIORITY:
            return int(text)

        if field is IssueOrderField.DUE_DATE:
            return date.fromisoformat(text)

        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise InvalidCursorError() from None

    if parsed.tzinfo is None:
        # A naive instant compares against TIMESTAMPTZ under the server's
        # timezone, so accepting one would make the resume point depend on
        # the connection rather than on the row it was minted from.
        raise InvalidCursorError()

    return parsed


def encode_issue_list_cursor(order: IssueOrder, key: OrderKey, issue_id: UUID) -> str:
    """A position in ONE ordering of the issue list.

    Three things rather than two, and the third is the whole reason this
    codec exists beside the generic one above. A keyset cursor is a row's
    coordinates in a particular sort; replay it under a different sort and
    the resume predicate compares the wrong column against the wrong value,
    which does not fail -- it returns a page, made of rows the client has
    already seen or rows it never will. Carrying the ordering lets
    `IssueService.list` refuse that instead of serving it.

    Signed by nothing, and it does not need to be: everything inside is
    either a value the client could read off the page it already has, or the
    ordering it just asked for. The workspace is deliberately NOT in here --
    it comes from the request's authorization, so a cursor minted in one
    workspace and replayed against another selects nothing.
    """
    return _encode_payload(
        {
            "o": order.token,
            "k": _order_key_text(key),
            "id": str(issue_id),
        }
    )


def decode_issue_list_cursor(cursor: str) -> IssueListCursor:
    """Decode an ordered issue cursor, or raise InvalidCursorError.

    The ordering is validated against the enums here rather than compared to
    the request's: what this raises for is a cursor that names an ordering
    that does not exist, which is malformed input. A cursor that names a real
    ordering the caller is not currently asking for is a different mistake
    with a different answer, and `IssueService` gives it.
    """
    payload = _decode_payload(cursor)

    raw_order = _string_field(payload, "o")
    raw_id = _string_field(payload, "id")
    raw_key = _optional_string_field(payload, "k")

    field_value, _, direction_value = raw_order.partition(":")

    try:
        order = IssueOrder(
            field=IssueOrderField(field_value),
            direction=OrderDirection(direction_value),
        )
        issue_id = UUID(raw_id)
    except (AttributeError, TypeError, ValueError):
        raise InvalidCursorError() from None

    return IssueListCursor(
        order=order,
        key=_parse_order_key(order.field, raw_key),
        id=issue_id,
    )


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


# The same three names, said without the word "issue" in them.
#
# Nothing above is issue-specific: the payload is a keyset position over
# `(created_at, id)`, which is the ordering every table in this schema is
# listed by. `projects` is the second list to need it, and importing
# `encode_issue_cursor` to page projects would read as a mistake at every call
# site -- while a second, identical implementation would be two encodings that
# have to stay byte-compatible by attention alone.
#
# Aliases rather than a rename, so that no existing caller or test changes in
# the same commit that adds a feature. Renaming the definitions and aliasing
# the old names is the tidier end state and belongs to whoever owns pagination.
KeysetCursor = IssueCursor
encode_keyset_cursor = encode_issue_cursor
decode_keyset_cursor = decode_issue_cursor

# The same three names again, for the alphabetical keyset rather than the
# chronological one.
#
# Nothing in the label codec is label-specific: the payload is a position over
# `(name, id)`, which is how any list of named things is read. `saved_views` is
# the second such list, and importing `encode_label_cursor` to page saved views
# would read as a mistake at every call site -- while a second, identical
# implementation would be two encodings that have to stay byte-compatible by
# attention alone. Aliases, for the same reason and with the same caveat as the
# three above.
NameCursor = LabelCursor
encode_name_cursor = encode_label_cursor
decode_name_cursor = decode_label_cursor
