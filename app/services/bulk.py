from collections.abc import Sequence
from uuid import UUID

import asyncpg

from app.domain.activity import ActivityKind, IssueSnapshot
from app.domain.bulk import BULK_MAX, BULK_MIN, NO_CHANGES, BulkIssuePatch
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.issues import UNSET, IssueEntity
from app.domain.tenancy import WorkspaceScope
from app.repositories.bulk import BulkRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.services import activity
from app.services.issues import ESTIMATE_MIN, PRIORITY_MAX, PRIORITY_MIN


# The foreign keys a bulk update can violate that are a client's to fix, and
# the field error each one becomes.
#
# The same discipline `IssueService` applies to its own mapping, and for the
# same reason: discriminating on the constraint name is what keeps this from
# turning an unexpected database error into a validation error. Each name here
# identifies exactly one rule about a value the client supplied, so the
# translation says only what the constraint already said. A foreign key absent
# from this mapping propagates untouched.
#
# Every one of these fails the WHOLE batch, which is the property this module
# exists for. "Move these twelve to In Progress" over a selection spanning two
# teams names a state most of them cannot be in; refusing all twelve is the
# right answer, and moving the ones that happened to fit would be a mutation
# nobody asked for.
_EXPECTED_FOREIGN_KEYS: dict[str, ValidationIssue] = {
    "issues_assignee_fk": ValidationIssue(
        field="assigneeId",
        code="NOT_A_MEMBER",
        message="Assignee must be a member of this workspace",
    ),
    "issues_workflow_state_fk": ValidationIssue(
        field="workflowStateId",
        code="NOT_FOUND",
        message=("Workflow state does not belong to every selected issue's team"),
    ),
    "issues_project_fk": ValidationIssue(
        field="projectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    "issues_milestone_fk": ValidationIssue(
        field="milestoneId",
        code="NOT_FOUND",
        message="Milestone not found in this project",
    ),
    "issues_cycle_fk": ValidationIssue(
        field="cycleId",
        code="NOT_FOUND",
        message="Cycle does not belong to every selected issue's team",
    ),
}

# What refuses archiving an issue that is still waiting in a triage queue.
# Migration 021 declares it so that a bulk archive cannot empty a team's queue
# as a side effect of somebody clearing a list.
_TRIAGE_ARCHIVE_CONSTRAINT = "issues_triage_is_not_archived"

# The one answer a batch containing an id this workspace does not hold must
# give -- and it is about the BATCH, not about the id.
#
# The message names no id and the code says nothing about which one failed.
# That is the whole isolation property of this module: a caller who could learn
# WHICH of a hundred ids was rejected could binary-search another tenant's
# issue ids at a hundred per request, learning nothing about the issues and
# everything about which ids are real. One id from elsewhere and one id that
# never existed produce this identically.
UNKNOWN_ISSUES = ValidationIssue(
    field="issueIds",
    code="NOT_FOUND",
    message="Some of these issues do not exist, or are no longer editable",
)


class BulkService:
    """One change applied to many issues, or to none of them.

    The service owns connection acquisition and transaction boundaries, and the
    workspace is threaded through every method rather than held on the
    instance, for the reasons `IssueService` sets out at length. Holding a
    scope is not permission to act in it.

    THE ALL-OR-NOTHING PROPERTY, stated once because every method here rests on
    it and none of them re-derives it.

    A bulk action is one act. Half of "move these twelve to Done" is a state
    nobody asked for and that no client can repair without knowing which half
    landed. So every method opens ONE transaction and every refusal inside it
    takes the whole batch down.

    The database gives most of that for free -- a foreign key violation aborts
    the statement, and the transaction rolls back. What it does NOT give is the
    refusal that is not an error: `UPDATE ... WHERE workspace_id = $1 AND id =
    ANY($2)` with one id from another tenant updates eleven rows and reports
    success. PostgreSQL has done exactly what it was asked. So every method
    here COUNTS: it locks the ids that resolve in this workspace, compares that
    count against the distinct ids it was given, and raises when they disagree
    -- inside the transaction, so the eleven legitimate updates roll back with
    the twelfth that never happened.

    That comparison is against DISTINCT ids and never against the raw list.
    `[a, a, b]` names two issues, and a caller that sent a duplicate must not
    be told its batch is unauthorised.

    THE AUTHORISATION RULE. Every id in the list is attacker-controlled, and
    the list is not checked by sampling. The lock in `BulkRepository.
    lock_snapshots` is scoped to the workspace, so it returns exactly the ids
    that are real HERE -- there is no path through this class in which an id is
    trusted because a different id in the same list was fine. Label ids get the
    same treatment through a separate count, for the same reason.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: BulkRepository,
        issue_labels: IssueLabelRepository,
    ):
        self._pool = pool
        self._repository = repository

        # Bulk label changes write `issue_labels`, and SQL against a table
        # belongs to the repository that owns it -- so this service reaches
        # across rather than `BulkRepository` growing statements about a join
        # table it does not own. `ProjectService` reaches for `IssueRepository`
        # on the same terms.
        self._issue_labels = issue_labels

    async def update_many(
        self,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
        patch: BulkIssuePatch = NO_CHANGES,
        add_label_ids: Sequence[UUID] = (),
        remove_label_ids: Sequence[UUID] = (),
        actor_id: UUID | None = None,
    ) -> list[IssueEntity]:
        """Apply one change to every named issue, or to none of them.

        Returns the issues as they now are, in no particular order -- the
        client sent the ids and can order them however it rendered them.

        The order of work inside the transaction is not arbitrary:

        1. lock and snapshot. This is the authorisation probe, the history read
           and the deadlock guard at once; `BulkRepository.lock_snapshots`
           explains all three. It runs FIRST so that nothing is written before
           the batch is known to be legitimate.
        2. verify the label ids, if any. A separate count because the bulk
           attach uses ON CONFLICT DO NOTHING and therefore cannot distinguish
           "already attached" from "no such label" in its own result.
        3. the column update, if the patch changes anything.
        4. the label writes.
        5. the history rows.

        Steps 3 and 4 are both optional and at least one of them has happened
        by the time the method returns, because `_validate` refuses a request
        that would do neither -- every write here stamps `updated_at`, so
        accepting an empty one would record an edit that changed nothing.

        A `ValidationError` raised anywhere inside the block aborts the
        transaction on the way out, which is what makes "one bad id mutates
        nothing" true rather than merely intended.
        """
        distinct_ids = self._validate(
            issue_ids=issue_ids,
            patch=patch,
            add_label_ids=add_label_ids,
            remove_label_ids=remove_label_ids,
        )

        async with self._pool.acquire() as connection:
            try:
                async with connection.transaction():
                    before = await self._lock(
                        connection,
                        scope=scope,
                        issue_ids=issue_ids,
                        distinct_ids=distinct_ids,
                    )

                    await self._verify_labels(
                        connection,
                        scope=scope,
                        label_ids=[*add_label_ids, *remove_label_ids],
                    )

                    if patch.is_empty:
                        # No column moved, so nothing is written to `issues`
                        # and `updated_at` is left where it was. The rows are
                        # still read back, because the payload carries the
                        # issues and a client re-selects `labels` on them.
                        after = await self._repository.touch_many(
                            connection,
                            scope=scope,
                            issue_ids=distinct_ids,
                        )
                    else:
                        after = await self._repository.update_many(
                            connection,
                            scope=scope,
                            issue_ids=distinct_ids,
                            patch=patch,
                        )

                    if add_label_ids:
                        await self._issue_labels.attach_many(
                            connection,
                            scope=scope,
                            issue_ids=distinct_ids,
                            label_ids=add_label_ids,
                        )

                    if remove_label_ids:
                        await self._issue_labels.detach_many(
                            connection,
                            scope=scope,
                            issue_ids=distinct_ids,
                            label_ids=remove_label_ids,
                        )

                    await self._record_changes(
                        connection,
                        scope=scope,
                        before=before,
                        after=after,
                        actor_id=actor_id,
                    )

                    return after
            except asyncpg.UniqueViolationError as exc:
                if exc.constraint_name != "issue_labels_exclusive_group_key":
                    raise

                raise ValidationError(
                    [
                        ValidationIssue(
                            field="addLabelIds",
                            code="EXCLUSIVE_GROUP",
                            message=(
                                "Some of these issues already have a label "
                                "from the same exclusive group"
                            ),
                        )
                    ]
                ) from None
            except asyncpg.ForeignKeyViolationError as exc:
                # Caught outside the transaction block so the rollback has
                # already happened by the time this runs. Catching inside it
                # would swallow the error and let the block commit.
                issue = _EXPECTED_FOREIGN_KEYS.get(exc.constraint_name or "")

                if issue is None:
                    raise

                raise ValidationError([issue]) from None

    async def archive_many(
        self,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
        actor_id: UUID | None = None,
    ) -> list[IssueEntity]:
        """Take every named issue off the board, or none of them.

        A separate method rather than a field on `BulkIssuePatch`, for the
        reason `IssuePatch` keeps `archived_at` out of itself: archiving is its
        own operation with its own authorisation story, not a field edit. It is
        also the only bulk action that is not reversible from the same screen,
        which is a good reason for it to be a different button and a different
        mutation.

        An already-archived id fails the whole batch rather than being counted
        as done. That is stricter than it strictly needs to be and it is the
        right strictness: a client selecting a list it had already archived
        half of is working from a stale view, and telling it so is more useful
        than a success that moved nothing.

        An issue still waiting in a triage queue is refused by migration 021's
        `issues_triage_is_not_archived`, and refusing it here rather than
        clearing the queue entry is deliberate -- emptying a team's incoming
        work as a side effect of somebody tidying a list is exactly the silent
        damage that constraint exists to prevent.
        """
        distinct_ids = self._validate_ids(issue_ids)

        async with self._pool.acquire() as connection:
            try:
                async with connection.transaction():
                    await self._lock(
                        connection,
                        scope=scope,
                        issue_ids=issue_ids,
                        distinct_ids=distinct_ids,
                    )

                    archived = await self._repository.archive_many(
                        connection,
                        scope=scope,
                        issue_ids=distinct_ids,
                    )

                    if len(archived) != len(distinct_ids):
                        # Unreachable through the lock above, which already
                        # counted the live ids -- and kept here anyway, because
                        # the two statements have different predicates and a
                        # future change to either could part them. The cost of
                        # being wrong is a partial archive nobody asked for.
                        raise ValidationError([UNKNOWN_ISSUES])

                    for entity in archived:
                        await activity.record(
                            connection,
                            scope=scope,
                            issue_id=entity.id,
                            actor_id=actor_id,
                            kind=ActivityKind.ARCHIVED,
                        )

                    return archived
            except asyncpg.CheckViolationError as exc:
                if exc.constraint_name != _TRIAGE_ARCHIVE_CONSTRAINT:
                    raise

                raise ValidationError(
                    [
                        ValidationIssue(
                            field="issueIds",
                            code="IN_TRIAGE",
                            message=(
                                "Some of these issues are still in triage; "
                                "accept or decline them first"
                            ),
                        )
                    ]
                ) from None

    async def _lock(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
        distinct_ids: list[UUID],
    ) -> dict[UUID, IssueSnapshot]:
        """Lock the batch and refuse it unless every id resolved here.

        The comparison is `len(locked) != len(distinct_ids)`, which is the
        whole authorisation of this module in one line. `lock_snapshots` is
        scoped to the workspace, so an id belonging to another tenant, an id
        that exists nowhere and an id that has been archived all fail to come
        back -- and any one of them fails the batch.

        `issue_ids` is passed as well as `distinct_ids` only so the lock takes
        the raw list; PostgreSQL's `= ANY` is a set membership test, so the
        duplicates cost nothing and removing them here would be a second place
        the two lists could disagree.
        """
        locked = await self._repository.lock_snapshots(
            connection,
            scope=scope,
            issue_ids=issue_ids,
        )

        if len(locked) != len(distinct_ids):
            raise ValidationError([UNKNOWN_ISSUES])

        return locked

    async def _verify_labels(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        label_ids: Sequence[UUID],
    ) -> None:
        """Refuse the batch unless every label id resolves in this workspace.

        Separate from the attach itself because `attach_many` uses ON CONFLICT
        DO NOTHING and so cannot tell "already attached" from "no such label"
        in its own row count. Inside the same transaction as the attach, so a
        label deleted between the two is refused by
        `issue_labels_exclusivity_fk` rather than silently skipped.

        Reports nothing about WHICH id failed, for the reason UNKNOWN_ISSUES
        gives about issue ids: a caller who could tell would have an existence
        oracle for another tenant's labels.
        """
        if not label_ids:
            return

        distinct = set(label_ids)

        found = await self._issue_labels.count_labels_in_workspace(
            connection,
            scope=scope,
            label_ids=list(distinct),
        )

        if found != len(distinct):
            raise ValidationError(
                [
                    ValidationIssue(
                        field="labelIds",
                        code="NOT_FOUND",
                        message="Some of these labels do not exist",
                    )
                ]
            )

    @staticmethod
    async def _record_changes(
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        before: dict[UUID, IssueSnapshot],
        after: Sequence[IssueEntity],
        actor_id: UUID | None,
    ) -> None:
        """One history row per field that actually moved, per issue.

        A loop over `activity.record_changes` rather than one set-based insert,
        and the cost is honest: a hundred issues moving two fields each is two
        hundred small inserts inside one transaction. The alternative is a
        multi-row statement whose parameter list is built at runtime and a
        second copy of `app.domain.activity.changes` written in SQL -- which
        would be the rule about what counts as a change living in two places,
        and the SQL copy is the one nobody would notice drifting.
        `activity.record_changes` already declines to write anything for a
        field that did not move, so a bulk action that set a priority every
        issue already had records nothing at all.

        # ponytail: N round trips per batch, bounded by BULK_MAX x 6 fields.
        # Batch the inserts if a bulk action ever shows up in a latency
        # profile; the shape to reach for is one INSERT ... SELECT over
        # unnest(), built from the same `changes` output.

        Nothing is recorded for the LABEL half of a bulk update. Migration 012
        gives `label_attached` and `label_detached` a `to_value` holding one
        label id, so a faithful record would be one row per issue per label --
        and `attach_many` deliberately does not report which of those actually
        landed, because ON CONFLICT DO NOTHING makes "already there" and "just
        added" indistinguishable in its result. Recording them all would claim
        attachments that were already there.
        """
        for entity in after:
            snapshot = before.get(entity.id)

            if snapshot is None:
                # Unreachable: `_lock` refused the batch unless every id came
                # back, and `after` is a subset of those ids. Skipping rather
                # than asserting, because a history row built from a missing
                # before-value would be a claim about the past nobody made.
                continue

            await activity.record_changes(
                connection,
                scope=scope,
                issue_id=entity.id,
                actor_id=actor_id,
                before=snapshot,
                after=IssueSnapshot.of(entity),
            )

    @staticmethod
    def _validate_ids(issue_ids: Sequence[UUID]) -> list[UUID]:
        """Bound the batch, and return the ids it names once each.

        A refusal and never a truncation. Applying a bulk action to the first
        hundred of a longer list would be a mutation the client did not ask for
        and cannot see, which is worse than the error -- the same argument
        every `first` validator in this codebase makes about never silently
        clamping a page size.

        Deduplicated with `dict.fromkeys` rather than `set`, because the order
        is what the caller sent and a set would make the statement's parameter
        -- and therefore the lock order and the returned rows -- depend on hash
        iteration. `BulkRepository.lock_snapshots` orders by id for its own
        reasons, so this is about reproducibility rather than about deadlocks.

        The bound is checked against the RAW list, before deduplication. A
        client that sent ten thousand ids has sent ten thousand ids, whatever
        they resolve to, and the request is refused for its size rather than
        quietly rescued by the duplicates in it.
        """
        if len(issue_ids) < BULK_MIN or len(issue_ids) > BULK_MAX:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="issueIds",
                        code="OUT_OF_RANGE",
                        message=(
                            f"Between {BULK_MIN} and {BULK_MAX} issues may be "
                            "changed at once"
                        ),
                    )
                ]
            )

        return list(dict.fromkeys(issue_ids))

    @classmethod
    def _validate(
        cls,
        *,
        issue_ids: Sequence[UUID],
        patch: BulkIssuePatch,
        add_label_ids: Sequence[UUID],
        remove_label_ids: Sequence[UUID],
    ) -> list[UUID]:
        """Everything the arguments alone decide, checked before a connection.

        Four rules, and every one of them is a request no state of the database
        could satisfy rather than a lookup that might miss:

        * the batch is bounded -- see `_validate_ids`;
        * the request changes something. An update that sets no column and
          moves no label is refused rather than treated as a no-op, because
          every write here stamps `updated_at` and accepting one would record
          an edit that changed nothing;
        * the three fields whose columns are NOT NULL cannot be cleared. GraphQL
          cannot express that: an input field is required exactly when it is
          non-null with no default, so a field that may be omitted is
          necessarily one that may arrive as null. Refusing it here turns what
          would otherwise be a NOT NULL violation from the driver into the
          field error it actually is -- `IssueService._validate_patch` makes the
          same move for the same reason;
        * a project and a milestone move together. `issues_milestone_fk` ties a
          milestone to the row's own project, so setting one without the other
          would leave a milestone belonging to the project the issue just left.
          Both present, or neither.

        Field order is deterministic so that clients can rely on it, and the
        codes and messages are a public contract.
        """
        distinct_ids = cls._validate_ids(issue_ids)

        issues: list[ValidationIssue] = []

        if patch.is_empty and not add_label_ids and not remove_label_ids:
            issues.append(
                ValidationIssue(
                    field="input",
                    code="EMPTY",
                    message="At least one change must be provided",
                )
            )

        for name, value in (
            ("workflowStateId", patch.workflow_state_id),
            ("priority", patch.priority),
        ):
            if value is None:
                issues.append(
                    ValidationIssue(
                        field=name,
                        code="NOT_NULLABLE",
                        message=f"{name} cannot be cleared",
                    )
                )

        if (patch.project_id is UNSET) != (patch.milestone_id is UNSET):
            issues.append(
                ValidationIssue(
                    field="milestoneId",
                    code="PROJECT_REQUIRED",
                    message=(
                        "projectId and milestoneId must be set together; send "
                        "milestoneId as null to place issues in a project "
                        "with no milestone"
                    ),
                )
            )

        # Three states to tell apart, not two: UNSET is "not moving the
        # project", None is "take these out of their project", and a value is
        # "put them in this one". Only the last of the three requires a
        # project, so `is not UNSET` alone would refuse the legitimate
        # `projectId: null, milestoneId: null` that clears both.
        if (
            patch.milestone_id is not UNSET
            and patch.milestone_id is not None
            and patch.project_id is None
        ):
            issues.append(
                ValidationIssue(
                    field="milestoneId",
                    code="PROJECT_REQUIRED",
                    message="A milestone can only be set together with its project",
                )
            )

        # The same two numbers and the same messages `IssueService` publishes
        # for these fields, reached through its constants rather than restated.
        # A client cannot tell which mutation refused it, so a bulk update that
        # reported a different range for `priority` than a single update would
        # be two contracts for one column.
        if isinstance(patch.priority, int) and not (
            PRIORITY_MIN <= patch.priority <= PRIORITY_MAX
        ):
            issues.append(
                ValidationIssue(
                    field="priority",
                    code="OUT_OF_RANGE",
                    message=(
                        f"Priority must be between {PRIORITY_MIN} and {PRIORITY_MAX}"
                    ),
                )
            )

        if isinstance(patch.estimate, int) and patch.estimate < ESTIMATE_MIN:
            issues.append(
                ValidationIssue(
                    field="estimate",
                    code="OUT_OF_RANGE",
                    message=f"Estimate must be {ESTIMATE_MIN} or greater",
                )
            )

        if issues:
            raise ValidationError(issues)

        return distinct_ids
