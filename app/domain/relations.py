"""Sub-issues and issue relations, as the product talks about them.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL. In
particular the vocabulary below is the *product's* four names, not the three
values migrations/010_issue_relations.sql stores. Which of the four is
storable and which is a reading of the other end is a fact about the table
and belongs to the repository; every layer above this one is entitled to say
`blocked_by` and be understood.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.issues import IssueEntity


class RelationType(StrEnum):
    """The four names a client may use for a relation between two issues.

    A StrEnum rather than a plain Enum because these values are also the
    strings the database stores for three of the four, and a type that
    compares equal to its own stored spelling removes a conversion that
    would otherwise appear at every boundary. `BLOCKED_BY` is the exception
    and is deliberately given the value it would have had if it were stored,
    so that nothing can accidentally round-trip it into the table: the
    repository translates it, and `issue_relations_type_known` refuses it
    outright if the translation is ever skipped.
    """

    BLOCKS = "blocks"
    BLOCKED_BY = "blocked_by"
    RELATED = "related"
    DUPLICATE = "duplicate"


# Relations whose two ends are interchangeable, so that naming them in either
# order describes the same relationship.
#
# This is the product reading that migration 010's
# issue_relations_symmetric_ordered enforces, restated here because the
# service and the repository both need it and neither should be reading the
# other's copy. `duplicate` sits here because the vocabulary above names no
# inverse for it -- there is no `duplicate_of`, so a client has no way to
# express a direction, so storing one would be inventing information.
SYMMETRIC_TYPES = frozenset({RelationType.RELATED, RelationType.DUPLICATE})

# Each name read from the other end of the same edge. Symmetric types are
# their own inverse, which is what makes this a total mapping and lets
# callers invert unconditionally instead of asking first.
_INVERSES = {
    RelationType.BLOCKS: RelationType.BLOCKED_BY,
    RelationType.BLOCKED_BY: RelationType.BLOCKS,
    RelationType.RELATED: RelationType.RELATED,
    RelationType.DUPLICATE: RelationType.DUPLICATE,
}


def invert(relation_type: RelationType) -> RelationType:
    """The same relation, named from the other issue's point of view."""
    return _INVERSES[relation_type]


class RelationEndpoint(StrEnum):
    """Which end of an operation a repository failure refers to.

    Named from the CALLER's point of view, never from the stored row's. A
    symmetric relation is canonicalised on the way into the table, so the
    row's `source_issue_id` is frequently the issue the client called the
    target; a failure reported in the table's vocabulary would be attached
    to the wrong input field. The repository performs the swap, so the
    repository is what translates back.
    """

    SOURCE = "source"
    TARGET = "target"
    PARENT = "parent"


class RelatedIssueNotFoundError(Exception):
    """An operation named an issue that this workspace does not hold.

    Raised where a foreign key refused a write, so it means exactly what
    that key means: no issue with that id exists in this workspace. It
    deliberately cannot distinguish "no such issue anywhere" from "an issue
    that belongs to someone else", because the composite keys in migration
    010 cannot either -- which is the property that keeps a relation
    mutation from becoming an existence oracle for other tenants' ids.
    """

    def __init__(self, endpoint: RelationEndpoint):
        super().__init__(f"No such issue in this workspace: {endpoint}")

        self.endpoint = endpoint


class DuplicateRelationError(Exception):
    """This relation already exists between these two issues.

    Raised where `issue_relations_unique` refused the insert. Expected user
    input rather than a defect: two clients relating the same pair, or one
    client double-clicking, are ordinary events.
    """

    def __init__(self):
        super().__init__("Relation already exists")


@dataclass(frozen=True, slots=True)
class IssueRelationEntity:
    """One relation, read from one issue's side.

    `type` is named from the point of view of the issue the caller asked
    about, and `issue` is the one at the far end -- so the same stored row
    is a `BLOCKS` whose `issue` is B when read from A, and a `BLOCKED_BY`
    whose `issue` is A when read from B. The subject is not carried here
    because every caller supplied it to get this back; storing it would be
    a second copy of a value that cannot be anything else.
    """

    id: UUID
    type: RelationType
    issue: IssueEntity
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IssueRelationPage:
    """A keyset page of relations, shaped like IssuePage for the same reason.

    Separate from `IssuePage` rather than generic over its node type: the
    two carry different entities and are consumed by different GraphQL
    connections, and a shared generic would buy one saved dataclass at the
    cost of a type parameter in every signature that mentions either.
    """

    nodes: list[IssueRelationEntity]
    has_next_page: bool
    end_cursor: str | None
