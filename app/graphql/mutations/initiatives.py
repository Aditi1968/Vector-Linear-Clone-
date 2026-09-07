from typing import TypeVar

import strawberry
from strawberry.types import Info
from strawberry.types.unset import UnsetType as StrawberryUnsetType

from app.domain.errors import ValidationError
from app.domain.patch import UNSET, UnsetType
from app.graphql.inputs.initiative import (
    InitiativeClearParentInput,
    InitiativeCreateInput,
    InitiativeDeleteInput,
    InitiativeProjectInput,
    InitiativeSetParentInput,
    InitiativeUpdateInput,
    InitiativeUpdatePostInput,
)
from app.graphql.scope import authorized_scope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.initiative import (
    InitiativeDeletePayload,
    InitiativePayload,
    InitiativeStatusType,
    InitiativeType,
    InitiativeUpdatePayload,
    InitiativeUpdateType,
)


T = TypeVar("T")


def _patch(value: T) -> T | UnsetType:
    """Translate Strawberry's absent-field sentinel into the domain's.

    The two sentinels exist for the same reason and belong to different layers.
    `strawberry.UNSET` is what an omitted input field arrives as, and it is a
    Strawberry object; services and domain code must not import Strawberry
    (CLAUDE.md), so it stops here, exactly as `Info` and every other transport
    concern does.

    `isinstance` and not `is`: identical at runtime, but the input field is
    annotated `str | None` while carrying an `UnsetType` at runtime -- a lie
    Strawberry tells on purpose -- and an identity test between two types mypy
    believes cannot overlap is reported as a mistake under `strict_equality`.
    """
    if isinstance(value, StrawberryUnsetType):
        return UNSET

    return value


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class InitiativeMutation:
    """The write half of the initiatives API.

    Every resolver here has the same three lines of shape: resolve the tenant,
    call one service method, translate a ValidationError into the payload.
    Nothing decides anything -- which initiative may be written, what a legal
    status is, whether a re-parent would close a loop -- because all of that is
    in InitiativeService, where REST and workers reach it too.

    `except ValidationError` and nothing broader. An asyncpg failure, a bug or
    an outage propagates through GraphQL's normal error mechanism and is masked
    by app/graphql/schema.py; converting it into a field error would tell a
    client to fix input that was never the problem, behind a 200.
    """

    @strawberry.mutation
    async def initiative_create(
        self,
        info: Info,
        input: InitiativeCreateInput,
    ) -> InitiativePayload:
        # Resolved before the try, and outside it. A missing workspace is not
        # something the client's input can be corrected to fix, so catching it
        # here would report a server-side gap as a field error.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.initiative_service.create(
                scope=scope,
                name=input.name,
                description=input.description,
                status=input.status.value,
                target_date=input.target_date,
                owner_id=input.owner_id,
            )
        except ValidationError as exc:
            return InitiativePayload(initiative=None, errors=_errors(exc))

        return InitiativePayload(
            initiative=InitiativeType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def initiative_update(
        self,
        info: Info,
        input: InitiativeUpdateInput,
    ) -> InitiativePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        # `status` is patched before it is unwrapped, because the three cases
        # are three different values: UNSET stays UNSET, an explicit null stays
        # None (which the service rejects, since the column is NOT NULL), and a
        # real member becomes the string the column stores.
        status = _patch(input.status)

        try:
            entity = await info.context.initiative_service.update(
                scope=scope,
                initiative_id=input.id,
                name=_patch(input.name),
                description=_patch(input.description),
                status=(
                    status.value if isinstance(status, InitiativeStatusType) else status
                ),
                target_date=_patch(input.target_date),
                owner_id=_patch(input.owner_id),
            )
        except ValidationError as exc:
            return InitiativePayload(initiative=None, errors=_errors(exc))

        return InitiativePayload(
            initiative=InitiativeType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def initiative_delete(
        self,
        info: Info,
        input: InitiativeDeleteInput,
    ) -> InitiativeDeletePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            await info.context.initiative_service.delete(
                scope=scope,
                initiative_id=input.id,
            )
        except ValidationError as exc:
            return InitiativeDeletePayload(
                deleted_initiative_id=None,
                errors=_errors(exc),
            )

        return InitiativeDeletePayload(
            deleted_initiative_id=input.id,
            errors=[],
        )

    @strawberry.mutation
    async def initiative_project_add(
        self,
        info: Info,
        input: InitiativeProjectInput,
    ) -> InitiativePayload:
        """Put a project into an initiative. The headline capability.

        Called once per project, so an initiative spanning three projects is
        three of these. A cross-workspace pair is refused by PostgreSQL rather
        than by anything in this process; the service turns the refusal into a
        NOT_FOUND on the field that named the offending id.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.initiative_service.add_project(
                scope=scope,
                initiative_id=input.initiative_id,
                project_id=input.project_id,
            )
        except ValidationError as exc:
            return InitiativePayload(initiative=None, errors=_errors(exc))

        return InitiativePayload(
            initiative=InitiativeType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def initiative_project_remove(
        self,
        info: Info,
        input: InitiativeProjectInput,
    ) -> InitiativePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.initiative_service.remove_project(
                scope=scope,
                initiative_id=input.initiative_id,
                project_id=input.project_id,
            )
        except ValidationError as exc:
            return InitiativePayload(initiative=None, errors=_errors(exc))

        return InitiativePayload(
            initiative=InitiativeType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def initiative_set_parent(
        self,
        info: Info,
        input: InitiativeSetParentInput,
    ) -> InitiativePayload:
        """Nest one initiative under another.

        The only path that writes `initiatives.parent_initiative_id` upward,
        and therefore the only one inside the lock and the cycle and depth
        checks. `initiativeCreate` deliberately takes no parent; see its input.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.initiative_service.set_parent(
                scope=scope,
                initiative_id=input.initiative_id,
                parent_id=input.parent_initiative_id,
            )
        except ValidationError as exc:
            return InitiativePayload(initiative=None, errors=_errors(exc))

        return InitiativePayload(
            initiative=InitiativeType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def initiative_clear_parent(
        self,
        info: Info,
        input: InitiativeClearParentInput,
    ) -> InitiativePayload:
        """Move one initiative back to the top level.

        A separate mutation from `initiativeSetParent` rather than that one
        with a null, because the two have different guarantees -- attaching
        runs a lock, a cycle walk and a depth check, detaching runs none of
        them -- and one mutation whose guarantees depended on whether an
        argument was null would be a contract nobody could read off the schema.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.initiative_service.clear_parent(
                scope=scope,
                initiative_id=input.initiative_id,
            )
        except ValidationError as exc:
            return InitiativePayload(initiative=None, errors=_errors(exc))

        return InitiativePayload(
            initiative=InitiativeType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def initiative_update_post(
        self,
        info: Info,
        input: InitiativeUpdatePostInput,
    ) -> InitiativeUpdatePayload:
        """Report how an initiative is going, attributed to the viewer.

        The author is `scope.user_id` -- the membership row the database
        matched when it authorized this request -- and never an argument, so
        authorship and permission cannot come from two lookups that disagree.
        `initiative_updates_author_fk` is what makes that true of the database
        rather than merely of this file.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.initiative_service.post_update(
                scope=scope,
                initiative_id=input.initiative_id,
                health=input.health.value,
                body=input.body,
                author_id=scope.user_id,
            )
        except ValidationError as exc:
            return InitiativeUpdatePayload(update=None, errors=_errors(exc))

        return InitiativeUpdatePayload(
            update=InitiativeUpdateType.from_entity(entity),
            errors=[],
        )
