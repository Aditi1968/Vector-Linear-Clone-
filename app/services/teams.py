from collections import defaultdict
from uuid import UUID

import asyncpg

from app.domain.errors import TeamNotFoundError
from app.domain.teams import TeamWorkflow, WorkflowStateEntity
from app.domain.tenancy import WorkspaceScope
from app.repositories.teams import TeamRepository


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

    async def default_team_id(self, scope: WorkspaceScope) -> UUID:
        """The team new issues in this workspace are filed against.

        A single SELECT needs no explicit write transaction, so this
        acquires a connection without opening one, and releases it before
        deciding what the lookup means.

        A workspace with no teams raises rather than returning None. The
        caller's only use for the answer is an insert that cannot proceed
        without one, so an Optional here would move an unavoidable failure
        to a less informative place -- and the failure is real: a workspace
        with no teams is a tenant nobody can file work in, which is a
        provisioning defect, not a client mistake.
        """
        async with self._pool.acquire() as connection:
            team_id = await self._repository.find_oldest_id(connection, scope=scope)

        if team_id is None:
            raise TeamNotFoundError()

        return team_id

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
