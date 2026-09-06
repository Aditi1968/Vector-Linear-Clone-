from uuid import UUID

import strawberry
from strawberry.types import Info

from app.graphql.scope import authorized_scope
from app.graphql.types.cycle import CycleType


@strawberry.type
class CycleQuery:
    """Cycle reads, composed into the root Query by app/graphql/schema.py.

    A type of its own rather than fields on the root, so that the root stays
    a list of features rather than a pile of every resolver in the product.
    """

    @strawberry.field
    async def cycle(
        self,
        info: Info,
        workspace_slug: str,
        id: UUID,
    ) -> CycleType | None:
        """One cycle in a workspace the viewer belongs to, or null.

        The slug is authorized before the id selects anything. A cycle in
        another workspace resolves to null -- the same answer as an id that
        exists nowhere -- so this resolver cannot be used to ask whether
        someone else's cycle exists.
        """
        scope = await authorized_scope(info, workspace_slug)

        entity = await info.context.cycle_service.get_by_id(
            scope=scope,
            cycle_id=id,
        )

        if entity is None:
            return None

        return CycleType.from_entity(entity)

    @strawberry.field
    async def cycles(
        self,
        info: Info,
        workspace_slug: str,
        team_id: UUID,
    ) -> list[CycleType]:
        """One team's cycles, lowest number first.

        `teamId` is required. Every alternative -- a default team, all the
        workspace's cycles when it is omitted -- answers a question the
        client did not ask, and the second one answers it with rows from
        teams the client may not be looking at.

        The team is not trusted to name its own workspace. The statement is
        scoped by BOTH the authorized workspace and the team, so a team id
        belonging to another tenant selects nothing -- see
        `CycleRepository.list_for_team`. A team in another workspace and a
        team id that names nothing therefore both return an empty list rather
        than an error, which is what keeps this from reporting that someone
        else's team is real. The list is unpaginated; the repository records
        why, and what the keyset would be if that stops being true.
        """
        scope = await authorized_scope(info, workspace_slug)

        entities = await info.context.cycle_service.list_for_team(
            scope=scope,
            team_id=team_id,
        )

        return [CycleType.from_entity(entity) for entity in entities]
