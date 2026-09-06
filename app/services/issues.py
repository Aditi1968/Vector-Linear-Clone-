from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.issues import IssueEntity, IssuePatch, Unset
from app.domain.pagination import (
    InvalidCursorError,
    IssueCursor,
    IssuePage,
    decode_issue_cursor,
    encode_issue_cursor,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository


if TYPE_CHECKING:
    from app.services.teams import TeamService


TITLE_MIN_LENGTH = 1
TITLE_MAX_LENGTH = 500

PRIORITY_MIN = 0
PRIORITY_MAX = 4

ESTIMATE_MIN = 0

FIRST_MIN = 1
FIRST_MAX = 100

# The constraint migrations/008_cycles.sql declares over
# `(workspace_id, team_id, cycle_id)`. Named here because a violation is
# only expected input for the constraint it was expected from: a bare
# `except asyncpg.ForeignKeyViolationError` would also translate a future
# constraint's refusal into "that cycle does not exist", which is how a
# schema change becomes a wrong message to a client instead of a loud
# failure to the operator.
ISSUES_CYCLE_FK = "issues_cycle_fk"


# Foreign keys whose violation is a client mistake, and the field error each
# one becomes.
#
# Discriminating on the constraint name is what keeps this from being the
# thing CLAUDE.md forbids -- turning unexpected database errors into
# validation errors. Each name here identifies exactly one rule, and each of
# those rules is about a value the client supplied, so the translation says
# only what the constraint already said. A foreign key not in this mapping --
# `issues_team_fk`, `issues_creator_fk` -- is violated only by a value the
# server chose, which makes it a defect here and not a correction for the
# client to make; those propagate untouched.
#
# `issues_assignee_fk` is violated identically by a user who does not exist
# and by one who exists in another workspace, and both arrive here as this
# single message. That is not a limitation to work around: distinguishing
# them would answer "does this user id exist" for a caller who is only
# entitled to know about their own workspace.
_EXPECTED_FOREIGN_KEYS: dict[str, ValidationIssue] = {
    "issues_assignee_fk": ValidationIssue(
        field="assigneeId",
        code="NOT_A_MEMBER",
        message="Assignee must be a member of this workspace",
    ),
    "issues_workflow_state_fk": ValidationIssue(
        field="workflowStateId",
        code="NOT_FOUND",
        message="Workflow state does not belong to this issue's team",
    ),
}


# The two foreign keys migrations/009_projects.sql puts on `issues`, and the
# field error each one means. Keyed on the constraint name because both raise
# the same exception class and point a client at different halves of its
# request.
#
# Neither distinguishes "belongs to another workspace" from "does not exist":
# a project in another tenant breaks `issues_project_fk` exactly as a
# nonexistent id does, and both answer NOT_FOUND. That is the point -- telling
# them apart would confirm the existence of a resource the caller cannot see.
PROJECT_CONSTRAINT_ERRORS: dict[str, ValidationIssue] = {
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
}

ISSUE_NOT_FOUND = ValidationIssue(
    field="issueId",
    code="NOT_FOUND",
    message="Issue not found",
)


class IssueService:
    """Business rules for issues.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service
    also owns connection acquisition and transaction boundaries.

    The workspace is threaded through as an argument on every method rather
    than held on the instance. A service constructed per request could hold
    one and still be correct today, but the moment anything caches, shares
    or reuses a service -- a worker looping over tenants, a batch job, a
    module-level singleton -- an instance attribute becomes an ambient
    current workspace that the next operation inherits without asking. See
    WorkspaceScope on why tenant identity has to travel with the operation.

    Holding a scope is not permission to act in it. Nothing in this class
    checks that the caller belongs to the workspace it named; that check
    does not exist yet, and when it does it will not live here.

    The completed_at rule
    ---------------------
    `completed_at` is derived, never set. No input type carries it and no
    method here accepts it. The rule, applied on every write:

        completed_at is non-NULL if and only if the issue's workflow state
        is in a terminal category -- 'completed' or 'canceled'.

    with one refinement about which instant it holds:

      * entering a terminal category from a non-terminal one stamps now();
      * leaving a terminal category clears it to NULL;
      * moving between two terminal categories -- Done to Canceled -- keeps
        the timestamp already there, because the work stopped when it first
        stopped and relabelling why does not restart it;
      * a write that does not change the state leaves it as it is.

    Every one of those falls out of a single expression evaluated by the
    server (`COALESCE(completed_at, now())` when terminal, NULL otherwise),
    so there is no branch here for a caller to reach around. Categories are
    compared, never names: 'Done' is a label a team may rename or delete,
    'completed' is a category migration 005's CHECK constrains.

    A newly created issue needs no such expression. It starts in whatever
    `TeamService.default_workflow_state_id` returns, which is the team's
    `unstarted` state, so the rule's answer for it is NULL -- which is the
    column's default.

    The rule is enforced at the write and not by a database constraint
    because the two columns live in different tables -- a CHECK cannot join
    `workflow_states` -- and a trigger is the other option this project has
    deliberately not taken up yet. Recomputing on every write rather than
    only on state changes is what compensates: a row that somehow disagrees
    is repaired by its next update rather than keeping the disagreement.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: IssueRepository,
        teams: "TeamService",
    ):
        self._pool = pool
        self._repository = repository

        # Creating an issue needs two things only the team layer can answer:
        # the next number on that team's counter, and the state a new issue
        # starts in. Both have to be decided inside this service's
        # transaction, so the collaborator is a service rather than a
        # repository -- see `TeamService.allocate_issue_number` for why the
        # allocation cannot own a transaction of its own.
        #
        # Required, not defaulted. A default would let a context be built
        # whose `create` fails at the first attempt to file an issue rather
        # than at construction -- and the tests that never create would go
        # on passing, which is exactly how the missing number column reached
        # an integrated database in the first place.
        self._teams = teams

    async def get_by_id(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """One live issue from this workspace, or nothing.

        "Not in this workspace", "archived" and "does not exist" are the
        same answer on purpose; the repository explains why the distinction
        must not be observable.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(
                connection,
                scope=scope,
                issue_id=issue_id,
            )

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        title: str,
        description: str | None = None,
        priority: int = 0,
        assignee_id: UUID | None = None,
        creator_id: UUID | None = None,
        estimate: int | None = None,
        due_date: date | None = None,
    ) -> IssueEntity:
        """File one issue in this workspace, against this team.

        The team is a required argument rather than something resolved in
        here. A service that picked a default team would be choosing where
        another tenant's work lands, using a rule invisible at the call
        site; the caller that knows which team it means is the one that has
        to say so.

        `creator_id` is likewise passed in and never derived here. It
        records who filed the issue, so the only honest source for it is
        whatever authenticated the request -- the resolver reads
        `info.context.viewer()` and hands the answer down. It is None for an
        unauthenticated caller, which is a state the column already allows
        and which every issue predating `users` is in anyway. It is
        deliberately not a client-supplied field: a caller able to name the
        creator could forge authorship.

        There is no `workflow_state_id` parameter. A new issue goes wherever
        new work goes on its team, which is the team's `unstarted` state;
        filing directly into some other state is a move, and moves go
        through `update`.

        The allocation and the insert share one transaction, and the
        allocation is last before it. `TeamService.allocate_issue_number`
        holds a row lock on the team from the moment it runs until this
        transaction ends, serialising every other create on the same team
        for that span, so the less that sits between the two the better.
        Rollback carries the increment back with it, which is what keeps the
        numbering gapless after a failed create.
        """
        self._validate_create(title=title, priority=priority, estimate=estimate)

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block
            # will also carry the audit / sync / outbox writes.
            try:
                async with connection.transaction():
                    # Resolved before the number is claimed, deliberately.
                    # The allocation takes a row lock on the team that is
                    # held to the end of this transaction and serialises
                    # every other creation on the same team for that whole
                    # span, so the read that does not need the lock happens
                    # outside it.
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

                    return await self._repository.create(
                        connection,
                        scope=scope,
                        team_id=team_id,
                        number=number,
                        workflow_state_id=workflow_state_id,
                        title=title,
                        description=description,
                        priority=priority,
                        assignee_id=assignee_id,
                        creator_id=creator_id,
                        estimate=estimate,
                        due_date=due_date,
                    )
            except asyncpg.ForeignKeyViolationError as exc:
                # Caught outside the transaction block so the rollback has
                # already happened by the time this runs. Catching inside it
                # would swallow the error and let the block commit -- with
                # the counter incremented and no issue to show for it.
                error = _validation_error_for(exc.constraint_name)

                if error is None:
                    raise

                raise error from None

    async def update(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        patch: IssuePatch,
    ) -> IssueEntity | None:
        """Apply a patch to one live issue in this workspace, or nothing.

        None means the issue is not there to update -- nonexistent, another
        tenant's, or archived -- and those stay indistinguishable, exactly
        as they are for a read. An update is otherwise a probe: a caller
        holding a guessed id could learn which ids exist by watching which
        updates report a different kind of failure.

        A single statement does the whole job, so no transaction is opened.
        The patch is not read back and merged in here; see the repository
        for why that shape would lose concurrent edits.
        """
        self._validate_patch(patch)

        async with self._pool.acquire() as connection:
            try:
                return await self._repository.update(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                    patch=patch,
                )
            except asyncpg.ForeignKeyViolationError as exc:
                error = _validation_error_for(exc.constraint_name)

                if error is None:
                    raise

                raise error from None

    async def archive(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """Take one issue off this workspace's board, or answer nothing.

        Archival rather than deletion, for the reasons migration 006 sets
        out: the identifier `CORE-42` is the issue's name everywhere outside
        this database, and 005 never reissues a number, so a discarded row
        turns every reference to it into one that resolves to nothing.

        None covers "no such issue", "another tenant's" and "already
        archived" alike. The last one collapses into the others rather than
        reporting success a second time, which keeps `archived_at` the
        moment of archival rather than of the most recent attempt.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.archive(
                connection,
                scope=scope,
                issue_id=issue_id,
            )

    async def set_cycle(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        cycle_id: UUID | None,
    ) -> IssueEntity:
        """Put this workspace's issue in a cycle, or take it out of one.

        Two expected failures, both reported as structured field errors
        because both are things a client sent and can send differently:

        * the issue is not this workspace's, or is not there at all -- the
          UPDATE matches nothing and there is no second statement to ask why;
        * the cycle is not the issue's team's, or is not there at all --
          `issues_cycle_fk` refuses the statement, and the two cases are
          indistinguishable from here on purpose. Telling them apart would
          let a client holding one of its own issues discover which cycle
          ids exist in teams it cannot see.

        The FK translation is narrowed to that one constraint. The same
        UPDATE can in principle violate others, and a violation nobody
        predicted is not user input -- it goes out as the unexpected failure
        it is rather than as advice to the client about a field.
        """
        async with self._pool.acquire() as connection:
            # A write, so the service opens the transaction even though one
            # statement is all it holds today.
            async with connection.transaction():
                try:
                    entity = await self._repository.set_cycle(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        cycle_id=cycle_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    if error.constraint_name != ISSUES_CYCLE_FK:
                        raise

                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="cycleId",
                                code="NOT_FOUND",
                                message="Cycle not found",
                            )
                        ]
                    ) from None

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

    async def set_project(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        project_id: UUID | None,
        milestone_id: UUID | None,
    ) -> IssueEntity:
        """Move one issue into a project and milestone, or out of both.

        Both are supplied together, and passing None for both is how an issue
        leaves a project. There is no separate "clear" operation because there
        is no separate state: an issue's place in the plan is one pair of
        values, and the schema refuses three of the four combinations of
        present and absent.

        The one combination this can rule out without asking the database --
        a milestone with no project -- is checked here, because it is a
        property of the arguments alone. Nothing else is: whether the project
        is in this workspace, and whether the milestone belongs to that
        project, are facts about rows that can change between a check and a
        write, so they are left to `issues_project_fk` and
        `issues_milestone_fk` and translated from the constraint they name.

        `issues_milestone_requires_project` is deliberately absent from that
        translation. If the pre-check above is right, the constraint cannot
        fire; if it ever does, the two disagree, and that is a defect to
        surface as one rather than to report to a client as bad input.
        """
        self._validate_set_project(project_id=project_id, milestone_id=milestone_id)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    issue = await self._repository.set_project(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        project_id=project_id,
                        milestone_id=milestone_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    mapped = PROJECT_CONSTRAINT_ERRORS.get(error.constraint_name or "")

                    if mapped is None:
                        raise

                    raise ValidationError([mapped]) from None

        if issue is None:
            raise ValidationError([ISSUE_NOT_FOUND])

        return issue

    async def list(
        self,
        *,
        scope: WorkspaceScope,
        first: int,
        after: str | None,
        team_id: UUID | None = None,
    ) -> IssuePage:
        """Forward keyset page of one workspace's live issues, newest first.

        A single SELECT needs no explicit write transaction, so this
        acquires a connection without opening one.

        The cursor is not trusted to carry a workspace and could not be if
        it did: it is Base64 over JSON, readable and writable by anyone
        holding it. The scope comes from this call, so a cursor minted in
        one workspace and replayed against another selects nothing rather
        than resuming someone else's page.

        `team_id` narrows within the workspace and is not validated against
        it here. The repository ANDs it onto the tenant predicate, so a team
        from another workspace selects nothing -- which is the same empty
        page an id naming no team gets, and deliberately so: a service that
        checked the team first and raised would report that another tenant's
        team is real.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list(
                connection,
                scope=scope,
                team_id=team_id,
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

        return IssuePage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    @staticmethod
    def _validate_list(*, first: int, after: str | None) -> IssueCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an
        expected input error rather than a parser exception.
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

    @staticmethod
    def _validate_set_project(
        *,
        project_id: UUID | None,
        milestone_id: UUID | None,
    ) -> None:
        """The one thing about this operation the arguments alone decide.

        A milestone belongs to a project; asking for one without the other is
        not a lookup that might succeed, it is a request that cannot be
        satisfied by any state of the database. Rejecting it here means no
        connection is acquired for it, which is the same property
        `_validate_create` gives the create path.
        """
        if milestone_id is not None and project_id is None:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="milestoneId",
                        code="PROJECT_REQUIRED",
                        message="A milestone can only be set together with its project",
                    )
                ]
            )

    @staticmethod
    def _validate_create(*, title: str, priority: int, estimate: int | None) -> None:
        """Collect every violation, then raise once.

        Field order is deterministic (title, then priority, then estimate)
        so that clients can rely on it. The codes and messages are a public
        contract.
        """
        issues = [
            *_title_issues(title),
            *_priority_issues(priority),
            *_estimate_issues(estimate),
        ]

        if issues:
            raise ValidationError(issues)

    @staticmethod
    def _validate_patch(patch: IssuePatch) -> None:
        """The same field rules as a create, applied to whatever is present.

        Only fields the patch actually carries are checked. A field left
        UNSET is not being written, so validating it would mean rejecting an
        update for the state of a value the request never mentioned -- which
        would make an issue that predates a rule permanently uneditable.

        An empty patch is refused rather than treated as a no-op. Every
        write here stamps `updated_at`, so accepting one would record an
        edit that changed nothing, and "last modified" is a fact the product
        shows.

        An explicit null for `title`, `priority` or `workflowStateId` is
        refused too, and that check has to live here because GraphQL cannot
        express it. An input field is required exactly when it is non-null
        and has no default, so a field that may be omitted from a patch is
        necessarily one that may arrive as null -- see `IssueUpdateInput`.
        Refusing it here turns what would otherwise be a NOT NULL violation
        from the driver into the field error it actually is.
        """
        if patch.is_empty:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="input",
                        code="EMPTY",
                        message="At least one field must be provided",
                    )
                ]
            )

        issues: list[ValidationIssue] = []

        # Field order is deterministic and matches the input type's, so that
        # clients can rely on it; the codes and messages are a public
        # contract.
        for name, value in (
            ("title", patch.title),
            ("priority", patch.priority),
            ("workflowStateId", patch.workflow_state_id),
        ):
            if value is None:
                issues.append(
                    ValidationIssue(
                        field=name,
                        code="NOT_NULLABLE",
                        message=f"{name} cannot be cleared",
                    )
                )

        if isinstance(patch.title, str):
            issues.extend(_title_issues(patch.title))

        if isinstance(patch.priority, int):
            issues.extend(_priority_issues(patch.priority))

        if not isinstance(patch.estimate, Unset):
            issues.extend(_estimate_issues(patch.estimate))

        if issues:
            raise ValidationError(issues)


def _validation_error_for(constraint_name: str) -> ValidationError | None:
    """The field error this foreign key stands for, or None for the rest.

    Takes the constraint's name rather than the exception, so nothing about
    asyncpg reaches this function -- and so the caller keeps the original
    exception in hand for the None case, where a bare `raise` re-raises it
    with its traceback intact. A constraint absent from the mapping is not a
    rule about client input, and is left to be masked and logged with every
    other unexpected failure.
    """
    return (
        ValidationError([issue])
        if (issue := _EXPECTED_FOREIGN_KEYS.get(constraint_name)) is not None
        else None
    )


def _title_issues(title: str) -> list[ValidationIssue]:
    # The title is validated as supplied -- never trimmed or rewritten.
    if len(title) < TITLE_MIN_LENGTH:
        return [
            ValidationIssue(
                field="title",
                code="REQUIRED",
                message="Title is required",
            )
        ]

    if len(title) > TITLE_MAX_LENGTH:
        return [
            ValidationIssue(
                field="title",
                code="TOO_LONG",
                message=f"Title must be at most {TITLE_MAX_LENGTH} characters",
            )
        ]

    return []


def _priority_issues(priority: int) -> list[ValidationIssue]:
    if priority < PRIORITY_MIN or priority > PRIORITY_MAX:
        return [
            ValidationIssue(
                field="priority",
                code="OUT_OF_RANGE",
                message=f"Priority must be between {PRIORITY_MIN} and {PRIORITY_MAX}",
            )
        ]

    return []


def _estimate_issues(estimate: int | None) -> list[ValidationIssue]:
    """No upper bound, matching `issues_estimate_non_negative` in 006.

    A ceiling here would be a guess at the unit a team estimates in, and
    the schema declines to make that guess for the reasons 006 records.
    Clearing an estimate (None) is always allowed.
    """
    if estimate is not None and estimate < ESTIMATE_MIN:
        return [
            ValidationIssue(
                field="estimate",
                code="OUT_OF_RANGE",
                message=f"Estimate must be {ESTIMATE_MIN} or greater",
            )
        ]

    return []
