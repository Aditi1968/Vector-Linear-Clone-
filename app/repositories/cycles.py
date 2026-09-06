from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.cycles import CycleEntity
from app.domain.tenancy import WorkspaceScope


class CycleRepository:
    """SQL access for `cycles`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement here is scoped to one workspace, and the scope arrives
    as a required keyword argument -- never a default, never an attribute
    set once on the instance. IssueRepository sets out why at length; the
    short of it is that `scope=` appearing literally at every call site is
    what makes "does this query cross tenants" a question the call answers
    by itself.

    Nothing here pre-checks the team a cycle names, and nothing here reads
    a row back to decide whether a write was allowed. Both rules are
    enforced by migrations/008_cycles.sql -- `cycles_team_fk` for the team,
    `cycles_team_number_key` for per-team numbering -- so a violation
    arrives as an asyncpg error from the statement itself rather than from a
    separate SELECT that another transaction could invalidate between the
    look and the write. Translating those errors into something a client
    can act on is CycleService's job, not this class's.
    """

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        number: int,
        name: str | None,
        starts_at: datetime,
        ends_at: datetime,
    ) -> CycleEntity:
        """Insert one cycle into this workspace, against this team.

        Both tenancy columns are written explicitly and neither carries a
        database default, so an insert that omitted either is a NOT NULL
        violation rather than a row quietly filed against the wrong tenant.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO cycles (
                workspace_id,
                team_id,
                number,
                name,
                starts_at,
                ends_at
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING
                id,
                number,
                name,
                starts_at,
                ends_at,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            team_id,
            number,
            name,
            starts_at,
            ends_at,
        )

        return self._to_entity(row)

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        cycle_id: UUID,
    ) -> CycleEntity | None:
        """The cycle with this id in this workspace, or nothing.

        The workspace is part of the lookup rather than a check applied to
        the row afterwards, for the reason IssueRepository.get_by_id spells
        out: a cycle belonging to another tenant must answer exactly as an
        id that exists nowhere, and a fetch-then-compare has already read
        the other tenant's row into this process by the time it decides.
        """
        row = await connection.fetchrow(
            """
            SELECT
                id,
                number,
                name,
                starts_at,
                ends_at,
                created_at,
                updated_at
            FROM cycles
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            cycle_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def find_many_by_ids(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        cycle_ids: Sequence[UUID],
    ) -> list[CycleEntity]:
        """Every cycle in this workspace among these ids, in no set order.

        One statement for many ids, because the alternative is one statement
        per issue on any list that resolves `Issue.cycle`. Ids belonging to
        another workspace simply do not come back -- the tenant predicate
        leads here as everywhere -- so a caller cannot use a batch to learn
        anything a single lookup would have withheld.

        The result is a list rather than a dict keyed by the ids asked for:
        building that mapping means deciding what an absent id means, which
        is the caller's decision and not the repository's.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                number,
                name,
                starts_at,
                ends_at,
                created_at,
                updated_at
            FROM cycles
            WHERE workspace_id = $1 AND id = ANY($2::UUID[])
            """,
            scope.workspace_id,
            list(cycle_ids),
        )

        return [self._to_entity(row) for row in rows]

    async def list_for_team(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
    ) -> list[CycleEntity]:
        """One team's cycles in this workspace, lowest number first.

        Both tenancy columns are in the predicate although `team_id` alone
        would already select the same rows -- `cycles_team_fk` guarantees a
        team belongs to one workspace. Keeping `workspace_id` first is what
        makes the statement readable as scoped without tracing a foreign key
        to prove it, and it is the leading column of the index
        `cycles_team_number_key` creates, which then serves the ordering too.

        `number` is unique per team, so the order is total and needs no
        tiebreak. Unpaginated on purpose: a team accumulates a couple of
        dozen cycles a year, and every screen that wants them -- a picker, a
        sidebar -- wants all of them at once. If that ever stops being true
        the keyset is already indexed and already total.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                number,
                name,
                starts_at,
                ends_at,
                created_at,
                updated_at
            FROM cycles
            WHERE workspace_id = $1 AND team_id = $2
            ORDER BY number
            """,
            scope.workspace_id,
            team_id,
        )

        return [self._to_entity(row) for row in rows]

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        cycle_id: UUID,
        number: int,
        name: str | None,
        starts_at: datetime,
        ends_at: datetime,
    ) -> CycleEntity | None:
        """Rewrite one cycle's editable fields, or report it was not there.

        `team_id` is deliberately not among them. A cycle's team is its
        identity -- issues joined it because it was that team's -- so moving
        one is not an edit but a different operation, and one nothing has
        asked for. Leaving it out means no statement here can produce a
        cycle whose team disagrees with the issues already in it.

        `updated_at` is stamped in the statement rather than by a trigger;
        migrations/008_cycles.sql records why there is no trigger.

        None means no row matched the id in this workspace, which is the
        same answer for a cycle that never existed and a cycle belonging to
        someone else.
        """
        row = await connection.fetchrow(
            """
            UPDATE cycles
            SET
                number = $3,
                name = $4,
                starts_at = $5,
                ends_at = $6,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
                id,
                number,
                name,
                starts_at,
                ends_at,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            cycle_id,
            number,
            name,
            starts_at,
            ends_at,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        cycle_id: UUID,
    ) -> bool:
        """Delete one cycle from this workspace; whether there was one to delete.

        RETURNING rather than reading the command tag: the tag arrives as
        text that has to be parsed, and a row either came back or did not.

        Issues sitting in the cycle are not deleted and are not read here.
        `issues_cycle_fk` carries `ON DELETE SET NULL (cycle_id)`, so the
        server clears their membership inside this same statement -- under
        the same lock, so no issue can join the cycle between the clearing
        and the delete, which is exactly the window an application-side
        "unassign, then delete" would leave open.
        """
        row = await connection.fetchrow(
            """
            DELETE FROM cycles
            WHERE workspace_id = $1 AND id = $2
            RETURNING id
            """,
            scope.workspace_id,
            cycle_id,
        )

        return row is not None

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> CycleEntity:
        return CycleEntity(
            id=row["id"],
            number=row["number"],
            name=row["name"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
