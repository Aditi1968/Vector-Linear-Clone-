from uuid import UUID

import strawberry
from strawberry.types import Info

from app.graphql.types.cycle import CycleType


@strawberry.type
class CycleQuery:
    """Cycle reads, composed into the root Query by app/graphql/schema.py.

    A type of its own rather than fields on the root, so that the root stays
    a list of features rather than a pile of every resolver in the product.
    """

    @strawberry.field
    async def cycle(self, info: Info, id: UUID) -> CycleType | None:
        """One cycle in the request's workspace, or null.

        The workspace comes from the request, never from the document;
        app/graphql/tenancy.py holds where it comes from today and what
        replaces that. A cycle in another workspace resolves to null -- the
        same answer as an id that exists nowhere -- so this resolver cannot
        be used to ask whether someone else's cycle exists.
        """
        scope = await info.context.tenant.scope()

        entity = await info.context.cycle_service.get_by_id(
            scope=scope,
            cycle_id=id,
        )

        if entity is None:
            return None

        return CycleType.from_entity(entity)

    @strawberry.field
    async def cycles(self, info: Info, team_id: UUID) -> list[CycleType]:
        """One team's cycles, lowest number first.

        `teamId` is required. Every alternative -- a default team, all the
        workspace's cycles when it is omitted -- answers a question the
        client did not ask, and the second one answers it with rows from
        teams the client may not be looking at.

        A team in another workspace, and a team id that names nothing, both
        return an empty list rather than an error. The list is unpaginated;
        `CycleRepository.list_for_team` records why, and what the keyset
        would be if that stops being true.
        """
        scope = await info.context.tenant.scope()

        entities = await info.context.cycle_service.list_for_team(
            scope=scope,
            team_id=team_id,
        )

        return [CycleType.from_entity(entity) for entity in entities]
