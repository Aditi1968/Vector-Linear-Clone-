import re
from collections import defaultdict
from uuid import UUID

import asyncpg

from app.domain.errors import TeamNotFoundError, ValidationError, ValidationIssue
from app.domain.estimates import EstimateScale
from app.domain.teams import TeamWorkflow, WorkflowStateEntity
from app.domain.tenancy import (
    AuthorizedWorkspaceScope,
    WorkspaceScope,
    require_workspace_admin,
)
from app.repositories.teams import TeamRepository


NAME_MAX_LENGTH = 200

# `teams_key_format` in migration 005, restated so a client is told what is
# wrong with its key instead of receiving a masked CHECK violation. Uppercase,
# no hyphen and no leading digit, because the key is rendered `<key>-<number>`
# and either would make ENG-42 ambiguous to parse.
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{0,9}$")

# The one violation of `teams_workspace_key_unique` that is an ordinary
# consequence of client input rather than a defect. Keyed on the constraint
# name for the reason app/services/projects.py sets out: two constraints on one
# statement raise the same exception class and mean different things, so
# anything this service did not name is re-raised and masked rather than
# reported to a client as a correctable mistake.
TEAM_KEY_UNIQUE_CONSTRAINT = "teams_workspace_key_unique"


class TeamService:
    """Business rules for teams and their workflow states.

    The service owns connection acquisition and transaction boundaries, and
    it is the only layer that turns "the database returned nothing" into a
    domain answer.

    Every method takes a `WorkspaceScope` rather than a bare workspace id.
    That is a readability decision with a safety consequence: a UUID
    argument called `workspace_id` sitting next to a UUID argument called
    `team_id` can be transposed at a call site and nothing anywhere
    complains, where a `WorkspaceScope` cannot be passed as a team id at
    all. Holding a scope still grants nothing -- see `WorkspaceScope`.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: TeamRepository,
    ):
        self._pool = pool
        self._repository = repository

    async def list_workflows(self, scope: WorkspaceScope) -> list[TeamWorkflow]:
        """Every team in the workspace, each with its workflow states.

        Two queries, not one per team: the states are fetched for the whole
        set of team ids at once. Nothing here needs an explicit
        transaction -- both statements are reads, and the second is
        restricted to ids the first returned, so a team created or deleted
        between them can add no row and remove only rows this call would
        have grouped under a team it no longer lists.

        A workspace with no teams costs one query. Skipping the second is
        not an optimisation but a correctness point: `= ANY('{}')` matches
        nothing, so issuing it would be a round trip whose answer is known.
        """
        async with self._pool.acquire() as connection:
            teams = await self._repository.list_by_workspace(
                connection,
                scope.workspace_id,
            )

            if not teams:
                return []

            states = await self._repository.list_workflow_states(
                connection,
                scope.workspace_id,
                [team.id for team in teams],
            )

        by_team: defaultdict[UUID, list[WorkflowStateEntity]] = defaultdict(list)

        for state in states:
            by_team[state.team_id].append(state)

        # The repository's ORDER BY is preserved by the grouping above, so
        # each team's states stay in board order and the teams stay in key
        # order.
        return [
            TeamWorkflow(
                team=team,
                workflow_states=tuple(by_team[team.id]),
            )
            for team in teams
        ]

    async def create(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        name: str,
        key: str,
    ) -> TeamWorkflow:
        """Create a team with a usable board, or refuse.

        The board is the part that is easy to leave out and impossible to
        notice: `issues.workflow_state_id` is NOT NULL and resolved from the
        team's own states, so a team created without them accepts no issues at
        all, and the failure surfaces later as a masked error on an unrelated
        mutation. Both writes are therefore one transaction -- a team with no
        board is not a halfway result, it is a team nobody can use.

        Requires admin or owner. The scope carries the role because only a
        lookup in `workspace_members` can produce one; see
        AuthorizedWorkspaceScope.

        The states are read back rather than reconstructed from the constant
        that seeded them, so the payload describes rows that exist with the
        ids a client will use.
        """
        require_workspace_admin(scope)

        self._validate(name=name, key=key)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    team = await self._repository.create(
                        connection,
                        workspace_id=scope.workspace_id,
                        name=name.strip(),
                        key=key,
                    )
                except asyncpg.UniqueViolationError as exc:
                    if exc.constraint_name != TEAM_KEY_UNIQUE_CONSTRAINT:
                        raise

                    # `from None`: asyncpg's error carries the offending row in
                    # its `detail`, and chaining it would carry that into every
                    # traceback and log line above here.
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="key",
                                code="KEY_TAKEN",
                                message="A team in this workspace already uses this key",
                            )
                        ]
                    ) from None

                await self._repository.seed_default_workflow_states(
                    connection,
                    workspace_id=scope.workspace_id,
                    team_id=team.id,
                )

                states = await self._repository.list_workflow_states(
                    connection,
                    scope.workspace_id,
                    [team.id],
                )

        return TeamWorkflow(team=team, workflow_states=tuple(states))

    @staticmethod
    def _validate(*, name: str, key: str) -> None:
        """Reject what the client can correct, before a connection is taken.

        The two checks below are the application's copy of
        `teams_name_present`-shaped intent and `teams_key_format`. They exist
        because a CHECK violation reaches a client as a masked internal error,
        which a form cannot render and a person cannot act on; the constraints
        remain the backstop for every write that does not come through here.
        """
        issues = []

        if not name.strip():
            issues.append(
                ValidationIssue(
                    field="name",
                    code="REQUIRED",
                    message="Name is required",
                )
            )
        elif len(name.strip()) > NAME_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="TOO_LONG",
                    message=f"Name must be at most {NAME_MAX_LENGTH} characters",
                )
            )

        if not KEY_PATTERN.match(key):
            issues.append(
                ValidationIssue(
                    field="key",
                    code="INVALID",
                    message=(
                        "Key must be 1-10 characters, uppercase letters and "
                        "digits, starting with a letter"
                    ),
                )
            )

        if issues:
            raise ValidationError(issues)

    async def allocate_issue_number(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
    ) -> int:
        """The next issue number for a team, claimed inside the caller's
        transaction.

        This is the one method on this service that takes a connection
        instead of acquiring one, and the exception is deliberate. The
        number and the issue that uses it have to be decided by the same
        transaction: allocate in a transaction of this service's own and
        the increment commits before the insert is attempted, so a failed
        insert leaves a number issued to nothing, and -- the real problem --
        the row lock that serialises concurrent allocation is released
        before the row that consumes the number exists.

        So the issue-creating service opens the transaction and hands its
        connection here:

            async with connection.transaction():
                number = await team_service.allocate_issue_number(
                    connection, scope=scope, team_id=team_id
                )
                await issue_repository.create(connection, ..., number=number)

        Allocate as late in that transaction as the insert allows. The lock
        is held from this statement until the transaction ends, and it
        serialises every other creation on the same team for that whole
        span -- but only on the same team, and only against other writers.

        A team that does not exist, or that exists in another workspace, is
        one answer: TeamNotFoundError. See that exception on why the two
        must stay indistinguishable.
        """
        number = await self._repository.allocate_issue_number(
            connection,
            scope.workspace_id,
            team_id,
        )

        if number is None:
            raise TeamNotFoundError()

        return number

    async def estimate_scale(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
    ) -> EstimateScale | None:
        """The unit this team's estimates count, or None if it is not here.

        Takes the caller's connection, like `allocate_issue_number` and
        `default_workflow_state_id` and for the milder of their two reasons:
        `IssueService.create` is about to write an estimate on the connection
        it already holds, and reading the rule that governs that write from a
        second one would be a second slot out of the pool for a single-column
        SELECT.

        Returns None rather than raising, which is the opposite of
        `default_workflow_state_id` beside it and is deliberate. That method's
        answer feeds a NOT NULL column, so an Optional would move an
        unavoidable failure somewhere less informative. This one's answer feeds
        a validation decision, and "no such team" is not a fact about the
        estimate -- the create raises `TeamNotFoundError` from the next call
        anyway, which keeps one answer for a team in another workspace rather
        than two that a caller could tell apart.
        """
        return await self._repository.find_estimate_scale(
            connection,
            scope.workspace_id,
            team_id,
        )

    async def set_estimate_scale(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        team_id: UUID,
        scale: EstimateScale,
    ) -> TeamWorkflow:
        """Choose what this team's estimates count.

        Requires admin or owner, unlike labels, cycles and templates, which any
        member may write. The difference is what the setting DOES: this one
        decides which estimates the whole team may write from now on, so a
        member switching it to t-shirt sizes would start refusing their
        colleagues' next edit. It is configuration in the same sense creating a
        team is, and `team_create` beside it is guarded the same way.

        THE ISSUES ALREADY ESTIMATED ARE LEFT ALONE. The scale bounds writes
        and not history -- see `TeamRepository.set_estimate_scale` and
        migration 029 -- so a team that moves to t-shirt sizes keeps the
        numbers its issues hold, and each conforms the next time somebody edits
        it. There is deliberately no confirmation step counting how many issues
        that is: it would be a scan of the whole board to warn about a state
        nothing is broken by.

        A team in another workspace and one that does not exist are the same
        `TeamNotFoundError`, on the same terms as every other method here.

        The scale arrives as an `EstimateScale` and not a string, so a value
        `teams_estimate_scale_known` would refuse cannot reach the statement --
        the GraphQL layer converts at the boundary and raises on a word it does
        not know.

        Answers a `TeamWorkflow` and not the bare entity, matching `create`, so
        that both mutations return the same `Team` shape. The board is read back
        in the same transaction rather than left empty: a payload whose
        `workflowStates` were an empty list would be a client's cue to blank the
        board it is rendering, and one query on an admin action nobody performs
        twice a year is cheaper than that bug.
        """
        require_workspace_admin(scope)

        async with self._pool.acquire() as connection:
            # A write, so the service owns the transaction -- and here it holds
            # two statements rather than one, so the states come back as they
            # are alongside the team that was just changed.
            async with connection.transaction():
                team = await self._repository.set_estimate_scale(
                    connection,
                    workspace_id=scope.workspace_id,
                    team_id=team_id,
                    scale=scale,
                )

                if team is None:
                    raise TeamNotFoundError()

                states = await self._repository.list_workflow_states(
                    connection,
                    scope.workspace_id,
                    [team.id],
                )

        return TeamWorkflow(team=team, workflow_states=tuple(states))

    async def default_workflow_state_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
    ) -> UUID:
        """The state a new issue on this team starts in.

        Takes the caller's connection for the same reason
        `allocate_issue_number` does, though not for the same stakes. There
        is no lock to hold here -- this is a read -- but the state it
        returns is written into a row by the caller's transaction, and
        reading it on a second connection would read outside that
        transaction's snapshot. A board being rebuilt concurrently could
        then hand back a state id that no longer exists by the time the
        insert runs, which the composite foreign key would refuse.

        A team with no `unstarted` state raises rather than returning None.
        The caller's only use for the answer is an insert into a NOT NULL
        column, so an Optional would move an unavoidable failure somewhere
        less informative. It is also a provisioning defect and not a client
        mistake: 005 seeds five states for every team, so a team without one
        was either created outside that path or had its board emptied.

        Raises TeamNotFoundError for a team in another workspace, on the
        same terms as every other method here -- the two must stay
        indistinguishable.
        """
        state_id = await self._repository.find_default_workflow_state_id(
            connection,
            scope.workspace_id,
            team_id,
        )

        if state_id is None:
            raise TeamNotFoundError()

        return state_id
