from uuid import UUID

import strawberry

from app.graphql.types.relations import IssueRelationTypeEnum


@strawberry.input
class IssueSetParentInput:
    """Make `issueId` a sub-issue of `parentId`.

    Both are required and neither is nullable, which is why setting and
    clearing are two mutations rather than one taking an optional parent.
    An optional field cannot distinguish "explicitly null, meaning detach"
    from "absent, meaning leave alone" without Strawberry's UNSET sentinel
    leaking into every caller and every test, and the ambiguity would sit on
    the one mutation whose failure mode is silently doing nothing.
    """

    issue_id: UUID
    parent_id: UUID


@strawberry.input
class IssueClearParentInput:
    """Detach `issueId` from whatever parent it has, if any."""

    issue_id: UUID


@strawberry.input
class IssueRelationCreateInput:
    """Relate two issues.

    `type` is read from the SOURCE's point of view: `BLOCKED_BY` here means
    the source is blocked by the target. The row that gets stored may be the
    other way round -- migration 010 keeps one canonical row per
    relationship -- but nothing about that reaches this input or the payload
    it produces.
    """

    source_issue_id: UUID
    target_issue_id: UUID
    type: IssueRelationTypeEnum


@strawberry.input
class IssueRelationDeleteInput:
    """Remove one relation by its own id.

    By id rather than by (source, target, type). The id is what
    `Issue.relations` returns from either end, so a client deletes what it
    is looking at instead of reconstructing a triple it would have to
    canonicalise correctly to match.
    """

    id: UUID
