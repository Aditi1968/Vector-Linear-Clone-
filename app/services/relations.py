from uuid import UUID

import asyncpg

from app.domain.activity import ActivityKind
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.issues import IssueEntity
from app.domain.notifications import NotificationKind
from app.domain.pagination import (
    InvalidCursorError,
    IssueCursor,
    IssuePage,
    decode_issue_cursor,
    encode_issue_cursor,
)
from app.domain.relations import (
    DuplicateRelationError,
    IssueRelationEntity,
    IssueRelationPage,
    RelatedIssueNotFoundError,
    RelationEndpoint,
    RelationType,
    invert,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.relations import RelationRepository
from app.services import activity


FIRST_MIN = 1

# The largest page either connection here will serve.
#
# Pinned to `app.graphql.limits.ASSUMED_PAGE_SIZE`, which is what the
# operation-limit rule charges for a page size it cannot read at validation
# time. If this were larger, a client could ask for more rows than the budget
# priced, and the complexity limit would be an under-charge rather than the
# over-charge it is documented to be. tests/test_relations.py asserts
# the relationship rather than leaving it to this comment.
FIRST_MAX = 100

# The client-facing input field each endpoint corresponds to. The repository
# reports which END of an operation a constraint refused, in the caller's
# vocabulary rather than the table's; this is the last step of turning that
# into an error a client can act on.
_ENDPOINT_FIELDS = {
    RelationEndpoint.SOURCE: "sourceIssueId",
    RelationEndpoint.TARGET: "targetIssueId",
    RelationEndpoint.PARENT: "parentId",
}


class RelationService:
    """Business rules for sub-issues and issue relations.

    The service owns connection acquisition and transaction boundaries, and
    the workspace is threaded through every method rather than held on the
    instance, for the reasons `IssueService` sets out at length.

    One rule here is not like the others, and it is the reason this class
    opens a transaction around what looks like a single UPDATE. Sub-issue
    cycles cannot be refused by a constraint -- no CHECK may read a second
    row -- so `set_parent` reads an ancestry and then writes, and those two
    steps have to be one atomic act or the guarantee is decorative. See
    `RelationRepository.lock_parenting` for what makes them one, and for the
    honest statement of what that does not cover.

    Everything else is refused by the database. Cross-workspace parenting,
    cross-workspace relating, self-parenting, self-relating and duplicate
    relations are all constraints in migration 010; the code below catches
    their refusals and names the field they belong to, and never checks the
    same rule twice with a SELECT of its own.

    Holding a scope is not permission to act in it. Nothing in this class
    checks that the caller belongs to the workspace it named.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: RelationRepository,
    ):
        self._pool = pool
        self._repository = repository

    # --- reads ---------------------------------------------------------

    async def find_parent(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """This issue's parent, or nothing.

        "The issue has no parent", "the issue does not exist" and "the issue
        belongs to another workspace" are one answer. The last of the three
        is the security property and the first two are what make it free:
        a caller cannot distinguish them, so the field cannot be used to
        probe for issues.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.find_parent(
                connection,
                scope=scope,
                issue_id=issue_id,
            )

    async def list_children(
        self,
        *,
        scope: WorkspaceScope,
        parent_id: UUID,
        first: int,
        after: str | None,
    ) -> IssuePage:
        """Forward keyset page of one issue's sub-issues, newest first.

        Returns `IssuePage` rather than a type of its own: a sub-issue is an
        issue, and a page of them differs from a page of issues only in
        which rows are in it.

        An issue that does not exist, and one in another workspace, both
        produce an empty page rather than an error. That is the same
        indistinguishability `find_parent` relies on, and it also means the
        field is total -- a client walking a stale list of ids gets empty
        pages, not a failed query.
        """
        cursor = self._validate_page(first=first, after=after)

        async with self._pool.acquire() as connection:
            rows = await self._repository.list_children(
                connection,
                scope=scope,
                parent_id=parent_id,
                limit=first + 1,
                after_created_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        has_next_page = len(rows) > first
        nodes = rows[:first]

        end_cursor = None

        if nodes:
            last = nodes[-1]
            end_cursor = encode_issue_cursor(last.created_at, last.id)

        return IssuePage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    async def list_relations(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        first: int,
        after: str | None,
    ) -> IssueRelationPage:
        """Forward keyset page of one issue's relations, newest first.

        Both directions in one page: the repository reads the rows where
        this issue is the stored source and the rows where it is the stored
        target, and names each one from this issue's side. A client asking
        an issue for its relations therefore sees `BLOCKED_BY` for the rows
        stored as somebody else's `blocks`, and never has to know which of
        the two ends the table happened to record.

        The cursor is the same `(created_at, id)` position the issue
        connections use, minted here over `issue_relations` rows. Its codec
        is named for issues because that is what it was written for; nothing
        in the payload is issue-specific, and the alternative -- a second
        Base64-over-JSON format differing only in the name of the function
        that writes it -- would be a second thing to get wrong.
        """
        cursor = self._validate_page(first=first, after=after)

        async with self._pool.acquire() as connection:
            rows = await self._repository.list_relations(
                connection,
                scope=scope,
                issue_id=issue_id,
                limit=first + 1,
                after_created_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        has_next_page = len(rows) > first
        nodes = rows[:first]

        end_cursor = None

        if nodes:
            # Built from the last RETURNED node, never from the extra row.
            last = nodes[-1]
            end_cursor = encode_issue_cursor(last.created_at, last.id)

        return IssueRelationPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    # --- writes --------------------------------------------------------

    async def set_parent(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        parent_id: UUID,
    ) -> IssueEntity:
        """Make one issue a sub-issue of another, in the same workspace.

        Three refusals, in the order they become knowable:

        * An issue cannot be its own parent. This is a comparison of two
          arguments -- it reads no row, so there is nothing to race, and it
          is settled before a connection is taken. `issues_parent_not_self`
          says the same thing in the database and remains the guarantee;
          this only produces the better message.
        * The proposed parent must not already be below this issue. That is
          the cycle guard, and unlike everything else here it is enforced by
          this code rather than by a constraint. It runs inside the
          transaction, after the workspace's parenting lock, so no
          concurrent re-parent can invalidate the ancestry it read.
        * Both issues must exist in this workspace. Not checked here at all:
          the UPDATE returning no row means the issue is absent, and
          `issues_parent_fk` refusing means the parent is.

        Cross-TEAM parenting is not among the refusals, deliberately. A
        sub-issue may belong to a different team than its parent as long as
        both sit in one workspace, which is why `issues_parent_fk` names
        `(workspace_id, parent_id)` and not the team.
        """
        if parent_id == issue_id:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="parentId",
                        code="SELF_PARENT",
                        message="An issue cannot be its own parent",
                    )
                ]
            )

        async with self._pool.acquire() as connection:
            # The transaction is load-bearing rather than conventional: the
            # advisory lock below is released at its end, and it must not be
            # released until the write it protects has committed.
            async with connection.transaction():
                await self._repository.lock_parenting(connection, scope=scope)

                if await self._repository.is_ancestor_or_self(
                    connection,
                    scope=scope,
                    issue_id=parent_id,
                    candidate_id=issue_id,
                ):
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="parentId",
                                code="CYCLE",
                                message=(
                                    "That issue is already a sub-issue of this one"
                                ),
                            )
                        ]
                    )

                try:
                    entity = await self._repository.set_parent(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        parent_id=parent_id,
                    )
                except RelatedIssueNotFoundError as exc:
                    raise self._not_found(exc.endpoint) from None

        if entity is None:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="issueId",
                        code="NOT_FOUND",
                        message="Issue not found",
                    )
                ]
            )

        return entity

    async def clear_parent(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity:
        """Detach one issue from its parent.

        No lock and no cycle check: removing an edge cannot close a loop.
        An issue that already has no parent is returned unchanged rather
        than reported as an error -- see `RelationRepository.clear_parent`.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                entity = await self._repository.clear_parent(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                )

        if entity is None:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="issueId",
                        code="NOT_FOUND",
                        message="Issue not found",
                    )
                ]
            )

        return entity

    async def create_relation(
        self,
        *,
        scope: WorkspaceScope,
        source_issue_id: UUID,
        target_issue_id: UUID,
        relation_type: RelationType,
        actor_id: UUID | None = None,
    ) -> IssueRelationEntity:
        """Relate two issues, and return the relation from the source's side.

        Self-relation is refused here for the same reason self-parenting is:
        it is a comparison of two arguments and needs no row. Everything
        else -- either issue missing or in another workspace, and the
        relation already existing -- is a constraint in migration 010 whose
        refusal is translated into the field it belongs to.

        The duplicate error is reported against `type` rather than against
        either id, because that is the field a client can usefully change:
        both issues are real and the pair is right; what already exists is
        this KIND of relation between them. And it is reported at all rather
        than treated as an idempotent success, because a client asking to
        relate two issues expects to learn that the relation was already
        there -- unlike clearing a parent, where the request names a state
        rather than an addition.
        """
        if source_issue_id == target_issue_id:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="targetIssueId",
                        code="SELF_RELATION",
                        message="An issue cannot be related to itself",
                    )
                ]
            )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    entity = await self._repository.create_relation(
                        connection,
                        scope=scope,
                        source_issue_id=source_issue_id,
                        target_issue_id=target_issue_id,
                        relation_type=relation_type,
                    )

                    # One history row per END, not one for the relationship.
                    # A relation is a fact about two issues and each of them
                    # has its own timeline, so an engineer reading the target
                    # sees "blocked_by SOURCE" rather than nothing at all.
                    # `invert` is what names the same edge from the other
                    # side; it is total, so this needs no branch.
                    for issue_id, other_id, named in (
                        (source_issue_id, target_issue_id, relation_type),
                        (target_issue_id, source_issue_id, invert(relation_type)),
                    ):
                        await activity.record(
                            connection,
                            scope=scope,
                            issue_id=issue_id,
                            actor_id=actor_id,
                            kind=ActivityKind.RELATION_ADDED,
                            from_value=named.value,
                            to_value=str(other_id),
                        )

                    blocked_id = _blocked_issue_id(
                        source_issue_id=source_issue_id,
                        target_issue_id=target_issue_id,
                        relation_type=relation_type,
                    )

                    if blocked_id is not None:
                        # Only the assignee, and only for a blocking
                        # relation. Being told that work you are holding has
                        # just been stopped is the one relation event worth
                        # interrupting somebody for; `related` and
                        # `duplicate` are context, and an inbox that carried
                        # them would be an inbox nobody reads.
                        await activity.notify(
                            connection,
                            scope=scope,
                            issue_id=blocked_id,
                            actor_id=actor_id,
                            kind=NotificationKind.BLOCKED,
                        )

                    return entity
                except RelatedIssueNotFoundError as exc:
                    raise self._not_found(exc.endpoint) from None
                except DuplicateRelationError:
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="type",
                                code="DUPLICATE",
                                message=(
                                    "That relation already exists between these issues"
                                ),
                            )
                        ]
                    ) from None

    async def delete_relation(
        self,
        *,
        scope: WorkspaceScope,
        relation_id: UUID,
    ) -> UUID:
        """Remove one relation, reporting an id this workspace does not hold.

        Deleting is not made idempotent, unlike clearing a parent. The two
        look similar and are not: an id names a specific row, so "it was not
        there" is information about the id the client sent, and answering
        success would let a client that deleted the wrong relation believe
        it had deleted the right one. A relation in another workspace
        produces this same answer, which is what keeps the mutation from
        confirming that someone else's relation id is real.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                deleted = await self._repository.delete_relation(
                    connection,
                    scope=scope,
                    relation_id=relation_id,
                )

        if deleted is None:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="id",
                        code="NOT_FOUND",
                        message="Relation not found",
                    )
                ]
            )

        return deleted

    # --- shared validation ---------------------------------------------

    @staticmethod
    def _not_found(endpoint: RelationEndpoint) -> ValidationError:
        """A missing-issue refusal, attached to the field the client sent.

        The message never distinguishes "no such issue" from "an issue you
        may not see", because the composite foreign keys that produced the
        refusal cannot distinguish them either. Making the message vaguer
        than the code would be pointless; making it sharper would be a
        cross-tenant existence oracle.
        """
        return ValidationError(
            [
                ValidationIssue(
                    field=_ENDPOINT_FIELDS[endpoint],
                    code="NOT_FOUND",
                    message="No such issue in this workspace",
                )
            ]
        )

    @staticmethod
    def _validate_page(*, first: int, after: str | None) -> IssueCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        The same contract `IssueService._validate_list` publishes, restated
        rather than imported: two services importing each other's private
        validators is a dependency between business rules that have no
        reason to move together, and the codes and messages here are a
        public contract in their own right.

        `first` is never silently clamped, and an invalid cursor is expected
        input rather than a parser exception.
        """
        issues: list[ValidationIssue] = []
        cursor: IssueCursor | None = None

        if first < FIRST_MIN or first > FIRST_MAX:
            issues.append(
                ValidationIssue(
                    field="first",
                    code="OUT_OF_RANGE",
                    message=f"first must be between {FIRST_MIN} and {FIRST_MAX}",
                )
            )

        if after is not None:
            try:
                cursor = decode_issue_cursor(after)
            except InvalidCursorError:
                issues.append(
                    ValidationIssue(
                        field="after",
                        code="INVALID_CURSOR",
                        message="Cursor is invalid",
                    )
                )

        if issues:
            raise ValidationError(issues)

        return cursor


def _blocked_issue_id(
    *,
    source_issue_id: UUID,
    target_issue_id: UUID,
    relation_type: RelationType,
) -> UUID | None:
    """Which of the two issues is now blocked, or neither.

    The direction is the content of a blocking relation and the two names
    say it from opposite ends: `BLOCKS` means the source stops the target,
    `BLOCKED_BY` means the target stops the source. Getting this backwards
    would notify the person who is holding things up rather than the person
    who is held up -- a mistake nothing downstream could detect, since both
    ids are real issues in the same workspace.

    None for `RELATED` and `DUPLICATE`, which name no direction at all;
    `SYMMETRIC_TYPES` says the same thing from the other side, and this
    matches the two directed names rather than excluding the symmetric ones
    so that a fifth relation type has to be classified here on purpose.
    """
    if relation_type is RelationType.BLOCKS:
        return target_issue_id

    if relation_type is RelationType.BLOCKED_BY:
        return source_issue_id

    return None
