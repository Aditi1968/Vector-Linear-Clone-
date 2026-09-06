from uuid import UUID

import asyncpg

from app.domain.errors import TeamNotFoundError
from app.domain.tenancy import WorkspaceScope
from app.repositories.teams import TeamRepository


class TeamService:
    """Business rules for teams.

    The service owns connection acquisition and transaction boundaries.

    There is one method, and it is a placeholder: until the product can name
    a team, something has to choose one for an issue to be filed against.
    See TeamRepository.find_oldest_id for the rule and for why it is a
    query rather than a constant.
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
