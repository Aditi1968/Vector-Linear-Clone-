from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.cycle import (
    CycleCreateInput,
    CycleUpdateInput,
    IssueSetCycleInput,
)
from app.graphql.scope import authorized_scope
from app.graphql.types.cycle import (
    CycleDeletePayload,
    CyclePayload,
    CycleType,
    IssueSetCyclePayload,
)
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueType


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class CycleMutation:
    """Cycle writes, composed into the root Mutation by app/graphql/schema.py.

    `issueSetCycle` lives here rather than beside `issueCreate` because it
    is the write this feature exists to enable: it changes an issue, but
    every rule it can break is a rule about cycles, and a reader asking what
    a cycle assignment is allowed to do should find the answer in one place.

    Every resolver here is the same four lines: resolve the tenant, call the
    service, translate ValidationError into the payload's `errors`, and let
    everything else propagate. Only expected input failures are translated;
    asyncpg outages, bugs and constraint violations nobody predicted go out
    through GraphQL's error mechanism, masked by app/graphql/schema.py.
    """

    @strawberry.mutation
    async def cycle_create(
        self,
        info: Info,
        input: CycleCreateInput,
    ) -> CyclePayload:
        # Resolved before the try, and outside it. A missing workspace is
        # not something the client's input can be corrected to fix, so
        # catching it here would report a server-side gap as a field error
        # on the input the client sent.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.cycle_service.create(
                scope=scope,
                team_id=input.team_id,
                number=input.number,
                name=input.name,
                starts_at=input.starts_at,
                ends_at=input.ends_at,
            )
        except ValidationError as exc:
            return CyclePayload(cycle=None, errors=_errors(exc))

        return CyclePayload(cycle=CycleType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def cycle_update(
        self,
        info: Info,
        input: CycleUpdateInput,
    ) -> CyclePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.cycle_service.update(
                scope=scope,
                cycle_id=input.id,
                number=input.number,
                name=input.name,
                starts_at=input.starts_at,
                ends_at=input.ends_at,
            )
        except ValidationError as exc:
            return CyclePayload(cycle=None, errors=_errors(exc))

        return CyclePayload(cycle=CycleType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def cycle_delete(
        self,
        info: Info,
        workspace_slug: str,
        id: UUID,
    ) -> CycleDeletePayload:
        """Delete a cycle. Issues in it survive and lose only their place.

        One of the two mutations that spell `workspaceSlug` as a field
        argument rather than on an input, because it takes no input object at
        all -- inventing a one-field one to carry a slug would be worse than
        the inconsistency.

        A cycle in another workspace answers NOT_FOUND, exactly as one that
        never existed: the delete is scoped, so it removes nothing, and the
        payload must not report which of the two happened.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            deleted_id = await info.context.cycle_service.delete(
                scope=scope,
                cycle_id=id,
            )
        except ValidationError as exc:
            return CycleDeletePayload(deleted_cycle_id=None, errors=_errors(exc))

        return CycleDeletePayload(deleted_cycle_id=deleted_id, errors=[])

    @strawberry.mutation
    async def issue_set_cycle(
        self,
        info: Info,
        input: IssueSetCycleInput,
    ) -> IssueSetCyclePayload:
        """Put an issue in a cycle, or take it out of one.

        A cycle belonging to another team -- or another tenant -- is refused
        by `issues_cycle_fk` inside the UPDATE itself, and reported here as
        a NOT_FOUND on `cycleId`. That is the same answer a cycle id naming
        nothing gets, deliberately: telling the two apart would let a client
        map another team's cycles by trying ids against its own issue.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.issue_service.set_cycle(
                scope=scope,
                issue_id=input.issue_id,
                cycle_id=input.cycle_id,
            )
        except ValidationError as exc:
            return IssueSetCyclePayload(issue=None, errors=_errors(exc))

        return IssueSetCyclePayload(
            issue=IssueType.from_entity(entity, scope),
            errors=[],
        )
