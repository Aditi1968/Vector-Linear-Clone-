from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg

from app.domain.activity import ActivityKind
from app.domain.errors import TeamNotFoundError, ValidationError, ValidationIssue
from app.domain.issues import IssueEntity
from app.domain.pagination import (
    InvalidCursorError,
    KeysetCursor,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from app.domain.relations import (
    DuplicateRelationError,
    RelatedIssueNotFoundError,
    RelationType,
)
from app.domain.tenancy import WorkspaceScope
from app.domain.triage import TriageIssuePage
from app.repositories.relations import RelationRepository
from app.repositories.triage import TriageRepository
from app.services import activity


if TYPE_CHECKING:
    from app.services.teams import TeamService


FIRST_MIN = 1

# The largest page this queue will serve.
#
# Pinned to `app.graphql.limits.ASSUMED_PAGE_SIZE`, which is what the
# operation-limit rule charges for a page size it cannot read at validation
# time. If this were larger, a client could ask for more rows than the budget
# priced and the complexity limit would be an under-charge rather than the
# over-charge it is documented to be. `RelationService` pins the same number
# for the same reason.
FIRST_MAX = 100

# The one answer every triage operation on an issue that is not in the queue
# must give.
#
# "No such issue", "an issue in another workspace", "an archived issue" and
# "an issue that is not in triage" all produce exactly this. The first three
# are the equivalence every other service here maintains, and the fourth joins
# them rather than getting a message of its own: a caller able to distinguish
# "not in triage" from "not yours" could enumerate another tenant's issue ids
# one request at a time, learning nothing about the issues and everything about
# which ids are real.
NOT_IN_TRIAGE = ValidationIssue(
    field="issueId",
    code="NOT_FOUND",
    message="Issue is not in triage",
)


class TriageService:
    """Business rules for a team's incoming-work queue.

    The service owns connection acquisition and transaction boundaries, and
    the workspace is threaded through every method rather than held on the
    instance, for the reasons `IssueService` sets out at length. Holding a
    scope is not permission to act in it.

    WHAT IS NOT HERE, and deliberately. The triage view offers actions to
    assign, prioritise, label and file an issue into a project, and none of
    them is a method on this class -- they are `issueUpdate`,
    `issueLabelAttach` and `issueSetProject`, which already exist and already
    carry their own validation, activity and tenancy rules. A triage-flavoured
    copy of each would be a second path into the same columns, and the second
    path is the one that forgets to record history.

    What IS here is the set of operations that only make sense for work nobody
    has accepted yet:

      * `enter` -- put an issue in a queue;
      * `accept` -- take it out, into a state somebody chose;
      * `decline` -- take it out, refused;
      * `mark_duplicate` -- decline it, recording which issue it repeats;
      * `change_team` -- send it to the team it should have been filed against,
        which renumbers it and is only defensible while it is still in a queue.

    `mark_duplicate` writes an `issue_relations` row of type `duplicate` and
    nothing else. There is no `duplicate_of` column and no triage-local
    duplicate table: migration 010 already owns the vocabulary, canonicalises
    the pair so that A-duplicates-B and B-duplicates-A are one row, and refuses
    the second insert. A second mechanism would be a second answer to "is this
    a duplicate" with nothing keeping the two in step.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: TriageRepository,
        relations: RelationRepository,
        teams: "TeamService",
    ):
        self._pool = pool
        self._repository = repository

        # Marking a duplicate writes a relation row in the SAME transaction as
        # the decline, so the repository is reached across to rather than the
        # triage repository growing statements about `issue_relations`. SQL
        # against a table belongs to the repository that owns it; the service
        # is what holds the boundary they are written inside. `ProjectService`
        # reaches for `IssueRepository` on the same terms.
        self._relations = relations

        # Changing an issue's team needs two things only the team layer can
        # answer, and both have to be decided inside this service's
        # transaction: the next number on the TARGET team's counter, and the
        # state a newly-arrived issue starts in there. The collaborator is a
        # service rather than a repository for the reason `IssueService` gives
        # -- the allocation cannot own a transaction of its own.
        self._teams = teams

    async def queue(
        self,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        first: int,
        after: str | None,
    ) -> TriageIssuePage:
        """Forward keyset page of one team's queue, oldest first.

        A single SELECT needs no explicit write transaction, so this acquires a
        connection without opening one.

        A team id from another workspace produces an empty page rather than an
        error, exactly as a team with an empty queue does. The repository ANDs
        the team predicate onto the tenant one, so an id from elsewhere narrows
        to nothing rather than widening to that workspace -- and a service that
        looked the team up first and raised would be reporting that another
        tenant's team is real.

        The cursor is not trusted to carry a workspace and could not be if it
        did: it is Base64 over JSON, readable and writable by anyone holding
        one. The scope comes from this call.
        """
        cursor = self._validate_page(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list_queue(
                connection,
                scope=scope,
                team_id=team_id,
                limit=first + 1,
                after_entered_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        has_next_page = len(rows) > first
        nodes = rows[:first]

        end_cursor = None

        if nodes:
            # Built from the last RETURNED node, never from the extra row.
            last = nodes[-1]
            end_cursor = encode_keyset_cursor(last.entered_at, last.issue.id)

        return TriageIssuePage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    async def waiting_count(
        self,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
    ) -> int:
        """How many issues are waiting in this team's queue."""
        async with self._pool.acquire() as connection:
            return await self._repository.count_queue(
                connection,
                scope=scope,
                team_id=team_id,
            )

    async def enter(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        actor_id: UUID | None = None,
    ) -> IssueEntity:
        """Put one issue into its own team's queue.

        There is no automatic entry on `issueCreate` yet, and this is the
        honest statement of that: work enters triage because somebody -- or,
        one day, an integration -- put it there. Wiring creation into a queue
        belongs in `IssueService.create`, where the team is already resolved,
        and it needs a per-team "triage is on" setting that no screen exists to
        change. This method is what the product has until then, and it is also
        the "Move to triage" action a person performs on an issue that turned
        out to need a decision.

        An issue already in a queue is refused rather than silently re-stamped,
        which keeps `triage_entered_at` the moment it arrived. The caller
        cannot tell that from an id that names nothing here; see NOT_IN_TRIAGE
        for why the four cases are one answer.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                entity = await self._repository.enter(
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
                                message="Issue not found, or already in triage",
                            )
                        ]
                    )

                # Recorded as a state change with no from-value, because that
                # is what it is: the issue's workflow state has not moved, its
                # standing has. `ActivityKind` is closed by migration 012's
                # `issue_activity_kind_known` and 021 does not widen it -- a
                # `triaged` kind would be a schema change, and reusing an
                # existing one honestly beats adding a thirteenth for an event
                # the timeline renders identically.
                await activity.record(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                    actor_id=actor_id,
                    kind=ActivityKind.STATE_CHANGED,
                    to_value=str(entity.workflow_state_id),
                )

                return entity

    async def accept(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        workflow_state_id: UUID,
        actor_id: UUID | None = None,
    ) -> IssueEntity:
        """Take one issue out of triage and into a state somebody chose.

        Two expected failures, both structured field errors because both are
        things a client sent and can send differently:

        * the issue is not in this workspace's triage -- see NOT_IN_TRIAGE for
          the four situations that collapse into that one answer;
        * the state is not the issue's team's, or is not there at all.
          `issues_workflow_state_fk` refuses the statement and the two cases
          are indistinguishable from here on purpose: telling them apart would
          let a client holding one of its own issues discover which state ids
          exist on teams it cannot see.

        The FK translation is narrowed to that one constraint. The same UPDATE
        can in principle violate others, and a violation nobody predicted is
        not user input -- it goes out as the unexpected failure it is rather
        than as advice to the client about a field.
        """
        async with self._pool.acquire() as connection:
            try:
                async with connection.transaction():
                    entity = await self._accept_locked(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        workflow_state_id=workflow_state_id,
                        actor_id=actor_id,
                    )
            except asyncpg.ForeignKeyViolationError as exc:
                # Caught outside the transaction block so the rollback has
                # already happened by the time this runs.
                if exc.constraint_name != "issues_workflow_state_fk":
                    raise

                raise ValidationError(
                    [
                        ValidationIssue(
                            field="workflowStateId",
                            code="NOT_FOUND",
                            message=(
                                "Workflow state does not belong to this issue's team"
                            ),
                        )
                    ]
                ) from None

        return entity

    async def decline(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        actor_id: UUID | None = None,
    ) -> IssueEntity:
        """Refuse one issue: out of triage, onto its team's canceled state.

        Nothing is deleted. A declined issue keeps its identifier and its
        history and is still readable by anyone who has a link to it, which is
        the same argument migration 006 makes for archiving rather than
        deleting -- and it is what lets somebody re-open it later by moving it
        to another state.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await self._decline_locked(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                    actor_id=actor_id,
                )

    async def mark_duplicate(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        duplicate_of_id: UUID,
        actor_id: UUID | None = None,
    ) -> IssueEntity:
        """Decline one issue, recording which issue it repeats.

        Two writes in one transaction: the relation row and the decline. The
        order is the relation FIRST, deliberately -- if the pair cannot be
        related (the other issue is not this workspace's, or the relation
        already exists) the decline must not have happened, and doing it the
        other way round would leave a rollback to undo something a client had
        already been told about in a previous request.

        The relation is an ordinary `duplicate` row in `issue_relations`, and
        there is deliberately no second mechanism. Migration 010 canonicalises
        symmetric types so that A-duplicates-B and B-duplicates-A are ONE row,
        which is why an issue already related this way is reported rather than
        stored twice, and why the relation reads correctly from both ends
        without triage having to know which end it wrote.

        Self-duplication is refused here rather than by the database, because
        it is a comparison of two arguments: it reads no row, so there is
        nothing to race, and it is settled before a connection is taken.
        `issue_relations_not_self` says the same thing in the schema and
        remains the guarantee; this only produces the better message.
        """
        if duplicate_of_id == issue_id:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="duplicateOfId",
                        code="SELF_DUPLICATE",
                        message="An issue cannot duplicate itself",
                    )
                ]
            )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    await self._relations.create_relation(
                        connection,
                        scope=scope,
                        source_issue_id=issue_id,
                        target_issue_id=duplicate_of_id,
                        relation_type=RelationType.DUPLICATE,
                    )
                except RelatedIssueNotFoundError:
                    # Which END was missing is not reported. The repository
                    # knows, and naming it would answer "does this id exist in
                    # my workspace" for an id the caller supplied -- which is
                    # exactly what the caller is entitled to know only about
                    # ids it already holds. Both ends are ids the caller sent,
                    # so one message for both is the honest answer.
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="duplicateOfId",
                                code="NOT_FOUND",
                                message="Issue not found",
                            )
                        ]
                    ) from None
                except DuplicateRelationError:
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="duplicateOfId",
                                code="ALREADY_RELATED",
                                message=(
                                    "These issues are already marked as duplicates"
                                ),
                            )
                        ]
                    ) from None

                await activity.record(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                    actor_id=actor_id,
                    kind=ActivityKind.RELATION_ADDED,
                    to_value=str(duplicate_of_id),
                )

                return await self._decline_locked(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                    actor_id=actor_id,
                )

    async def change_team(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        team_id: UUID,
        actor_id: UUID | None = None,
    ) -> IssueEntity:
        """Move a queued issue to the team it should have been filed against.

        The issue is RENUMBERED, which is the only place in this product where
        that happens and needs its argument made. Everything else here treats
        an identifier as permanent -- 006 archives rather than deletes because
        005 never reissues a number, and `ENG-42` is the issue's name in URLs,
        commit messages and conversation. An issue in triage has none of that
        history: nobody has accepted it, nothing links to it, and the team it
        was filed against was a guess made by whoever or whatever filed it.
        Correcting that guess is what this is, and
        `TriageRepository.change_team` refuses to do it to any issue that is
        not still waiting.

        The allocation is last before the write, and it is why the number
        cannot be resolved outside this transaction:
        `TeamService.allocate_issue_number` holds a row lock on the target team
        from the moment it runs until this transaction ends, serialising every
        creation and every other move onto that team for that span. The state
        lookup, which needs no lock, happens first.

        The issue stays in triage, on the new team's queue. Moving work to the
        right team is not deciding what to do with it; the receiving team makes
        that decision.

        A team id from another workspace never reaches the UPDATE: resolving
        the target team's default state raises `TeamNotFoundError` first, from
        a lookup that is already scoped to this workspace and that deliberately
        cannot distinguish "no such team" from "a team you may not see".
        `issues_team_fk` would refuse the write anyway, against the issue's own
        stored workspace, and remains the guarantee -- this only produces the
        better message and saves an allocation.
        """
        async with self._pool.acquire() as connection:
            try:
                async with connection.transaction():
                    workflow_state_id = await self._teams.default_workflow_state_id(
                        connection,
                        scope=scope,
                        team_id=team_id,
                    )

                    number = await self._teams.allocate_issue_number(
                        connection,
                        scope=scope,
                        team_id=team_id,
                    )

                    entity = await self._repository.change_team(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        team_id=team_id,
                        number=number,
                        workflow_state_id=workflow_state_id,
                    )

                    if entity is None:
                        # Raised rather than returned, so the transaction rolls
                        # back and the number allocated a moment ago goes back
                        # with it. Returning None here would commit the
                        # increment and leave a permanent hole in the target
                        # team's numbering -- the exact contract
                        # migrations/005_team_workflows.sql spells out for the
                        # counter.
                        raise ValidationError([NOT_IN_TRIAGE])

                    await activity.record(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        actor_id=actor_id,
                        kind=ActivityKind.STATE_CHANGED,
                        to_value=str(workflow_state_id),
                    )

                    return entity
            except TeamNotFoundError:
                # Caught outside the transaction block so the rollback has
                # already happened by the time this runs -- which matters here
                # more than usual, because a rollback is what returns an
                # allocated issue number to the counter.
                raise ValidationError(
                    [
                        ValidationIssue(
                            field="teamId",
                            code="NOT_FOUND",
                            message="Team not found",
                        )
                    ]
                ) from None

    async def _accept_locked(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        workflow_state_id: UUID,
        actor_id: UUID | None,
    ) -> IssueEntity:
        """The accept write and its history, on a caller's open transaction."""
        entity = await self._repository.accept(
            connection,
            scope=scope,
            issue_id=issue_id,
            workflow_state_id=workflow_state_id,
        )

        if entity is None:
            raise ValidationError([NOT_IN_TRIAGE])

        await activity.record(
            connection,
            scope=scope,
            issue_id=issue_id,
            actor_id=actor_id,
            kind=ActivityKind.STATE_CHANGED,
            to_value=str(entity.workflow_state_id),
        )

        return entity

    async def _decline_locked(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        actor_id: UUID | None,
    ) -> IssueEntity:
        """The decline write and its history, on a caller's open transaction.

        Shared by `decline` and `mark_duplicate` rather than the second calling
        the first, because the first would open a transaction of its own: the
        relation row and the decline have to commit together or a duplicate
        marking can leave a relation with no decline behind it.

        A team with no `canceled` workflow state at all makes the repository's
        subquery NULL, which the NOT NULL column refuses. That is a workspace
        configuration problem rather than bad client input, and it is still
        reported as a field error because the caller has to be told something
        it can act on -- and because the alternative, a masked "Internal server
        error", would send an operator looking for a bug in this service.
        """
        try:
            entity = await self._repository.decline(
                connection,
                scope=scope,
                issue_id=issue_id,
            )
        except asyncpg.NotNullViolationError as exc:
            if exc.column_name != "workflow_state_id":
                raise

            raise ValidationError(
                [
                    ValidationIssue(
                        field="issueId",
                        code="NO_CANCELED_STATE",
                        message=(
                            "This issue's team has no canceled workflow state "
                            "to decline into"
                        ),
                    )
                ]
            ) from None

        if entity is None:
            raise ValidationError([NOT_IN_TRIAGE])

        # `record` and not `record_changes`, and no snapshot is taken first.
        # `record_changes` needs a before-value read under a row lock, which is
        # the right shape for a general edit whose caller does not know which
        # fields moved; this method knows exactly one field moved and knows
        # where to. Reading the row first to say what it moved FROM would be a
        # second statement to record something the timeline renders as "moved
        # to Canceled" either way.
        await activity.record(
            connection,
            scope=scope,
            issue_id=issue_id,
            actor_id=actor_id,
            kind=ActivityKind.STATE_CHANGED,
            to_value=str(entity.workflow_state_id),
        )

        return entity

    @staticmethod
    def _validate_page(*, first: int, after: str | None) -> KeysetCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an expected
        input error rather than a parser exception.
        """
        issues: list[ValidationIssue] = []
        cursor: KeysetCursor | None = None

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
                cursor = decode_keyset_cursor(after)
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
