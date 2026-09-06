"""Transport types for sub-issues and relations.

Two naming notes, because this module holds a pair that is easy to confuse.
`IssueRelationTypeEnum` is the *enum* of relation kinds and is published as
`IssueRelationType`; `IssueRelationType` is the *object* type for one
relation and is published as `IssueRelation`. The Python names follow this
package's `...Type` convention and the GraphQL names follow what reads best
in a document, so the two disagree here and only here.

Nothing in this module imports `IssueType`, deliberately. It is imported BY
`app.graphql.types.issue`, and an import back the other way would be a cycle
-- but the real reason is the one below: no type here may lead to a type that
leads back to an issue.
"""

import enum
from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.issues import IssueEntity
from app.domain.pagination import IssuePage
from app.domain.relations import IssueRelationEntity, IssueRelationPage, RelationType
from app.graphql.types.pagination import PageInfo


# Page size for `Issue.children` and `Issue.relations` when a document does
# not say.
#
# Ten rather than the fifty `Query.issues` defaults to, and the difference is
# arithmetic rather than taste. `app.graphql.limits` prices a nested list
# field at (outer page size) x (inner page size) x (fields below it), and
# charges the SCHEMA's default for any document that omits the argument. At
# fifty, one page of issues asking each for its sub-issues costs
# 25 x 50 x 2 = 2500 against a budget of 1000 and is refused outright; at ten
# the same document costs 500 and runs. A default that made the ordinary
# nested query unaskable would be a limit doing damage rather than work.
#
# It bounds real fan-out too, not just the number. These fields resolve one
# query per parent issue -- see the note on `IssueType.children` -- so the
# budget is also what caps how many of those a single document can buy.
DEFAULT_RELATED_FIRST = 10


@strawberry.enum(name="IssueRelationType")
class IssueRelationTypeEnum(enum.Enum):
    """The four names a client may use for a relation.

    Declared here rather than by decorating `app.domain.relations.RelationType`
    with `strawberry.enum`, which would work and would put a Strawberry
    attribute on a domain class -- the one thing the domain layer is not
    allowed to carry. The values are kept identical to the domain enum's so
    the translation below is a lookup rather than a table, and
    tests/test_relations.py fails if the two ever drift.

    `BLOCKED_BY` is a name, not a stored value: migration 010 keeps one row
    per relationship and produces this reading from the other end. A client
    may create with it, filter on it and receive it; the table never sees it.
    """

    BLOCKS = "blocks"
    BLOCKED_BY = "blocked_by"
    RELATED = "related"
    DUPLICATE = "duplicate"

    def to_domain(self) -> RelationType:
        return RelationType(self.value)

    @classmethod
    def from_domain(cls, relation_type: RelationType) -> "IssueRelationTypeEnum":
        return cls(relation_type.value)


@strawberry.type(name="IssueSummary")
class IssueSummaryType:
    """An issue at the far end of an edge: every scalar field, no edges.

    The same row `Issue` describes, and deliberately a different type. It is
    what makes the graph this schema exposes ACYCLIC by construction: nothing
    reachable from here returns an issue, so `parent { children { parent
    { ... } } }` is not a query a client can write and then have refused --
    it is not a query that parses.

    That is a stronger guarantee than the depth limit, which would also have
    caught it. `MAX_DEPTH = 10` refuses a document at ten levels; it does not
    stop ten levels of resolution from being ten rounds of database queries
    fanning out, and it is a number someone may reasonably raise one day for
    an unrelated reason. A schema with no cycle in it cannot be nested at
    all, and no future edit to a limit can change that.

    The cost is that a client wanting a sub-issue's own sub-issues issues a
    second query for it, keyed by the id below. For a tree the product
    reveals one level at a time, that is the same number of round trips
    either way.
    """

    id: UUID
    title: str
    description: str | None
    priority: int
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: IssueEntity) -> "IssueSummaryType":
        return cls(
            id=entity.id,
            title=entity.title,
            description=entity.description,
            priority=entity.priority,
            completed_at=entity.completed_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type(name="IssueRelation")
class IssueRelationType:
    """One relation, named from the point of view of the issue that owns it.

    `issue` is the far end and `type` reads from the near end, so the single
    row recording "A blocks B" is `{type: BLOCKS, issue: B}` under A and
    `{type: BLOCKED_BY, issue: A}` under B. Neither side is more real than
    the other, and neither is the stored one as far as a client can tell.

    `id` is the relation's own id and is the handle `issueRelationDelete`
    takes. It is the same id from both sides -- there is one row -- so
    deleting through either end removes the relationship for both, which is
    the behaviour a row-per-direction table could not have offered without
    two deletes and a way to fail between them.
    """

    id: UUID
    type: IssueRelationTypeEnum
    issue: IssueSummaryType
    created_at: datetime

    @classmethod
    def from_entity(cls, entity: IssueRelationEntity) -> "IssueRelationType":
        return cls(
            id=entity.id,
            type=IssueRelationTypeEnum.from_domain(entity.type),
            issue=IssueSummaryType.from_entity(entity.issue),
            created_at=entity.created_at,
        )


@strawberry.type
class IssueSummaryConnection:
    nodes: list[IssueSummaryType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: IssuePage) -> "IssueSummaryConnection":
        return cls(
            nodes=[IssueSummaryType.from_entity(entity) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


@strawberry.type
class IssueRelationConnection:
    nodes: list[IssueRelationType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: IssueRelationPage) -> "IssueRelationConnection":
        return cls(
            nodes=[IssueRelationType.from_entity(entity) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )
