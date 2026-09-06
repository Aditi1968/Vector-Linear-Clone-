from typing import TypeVar

import strawberry
from strawberry.types import Info
from strawberry.types.unset import UnsetType as StrawberryUnsetType

from app.domain.errors import ValidationError
from app.domain.patch import UNSET, UnsetType
from app.graphql.inputs.project import (
    IssueSetProjectInput,
    ProjectCreateInput,
    ProjectDeleteInput,
    ProjectMilestoneCreateInput,
    ProjectMilestoneDeleteInput,
    ProjectMilestoneUpdateInput,
    ProjectTeamInput,
    ProjectUpdateInput,
)
from app.graphql.scope import authorized_scope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueSetProjectPayload, IssueType
from app.graphql.types.project import (
    ProjectDeletePayload,
    ProjectMilestoneDeletePayload,
    ProjectMilestonePayload,
    ProjectMilestoneType,
    ProjectPayload,
    ProjectStateType,
    ProjectType,
)


T = TypeVar("T")


def _patch(value: T) -> T | UnsetType:
    """Translate Strawberry's absent-field sentinel into the domain's.

    The two sentinels exist for the same reason and belong to different
    layers. `strawberry.UNSET` is what an omitted input field arrives as, and
    it is a Strawberry object; services and domain code must not import
    Strawberry (CLAUDE.md), so it stops here, exactly as `Info`, `UUID`
    coercion and every other transport concern does.

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
class ProjectMutation:
    """The write half of the projects API.

    Every resolver here has the same three lines of shape: resolve the tenant,
    call one service method, translate a ValidationError into the payload.
    Nothing decides anything -- which project may be written, what a legal
    state is, what happens to an issue when its project is deleted -- because
    all of that is in ProjectService, where REST and workers reach it too.

    `except ValidationError` and nothing broader. An asyncpg failure, a bug or
    an outage propagates through GraphQL's normal error mechanism and is
    masked by app/graphql/schema.py; converting it into a field error would
    tell a client to fix input that was never the problem, behind a 200.
    """

    @strawberry.mutation
    async def project_create(
        self,
        info: Info,
        input: ProjectCreateInput,
    ) -> ProjectPayload:
        # Resolved before the try, and outside it. A missing workspace is not
        # something the client's input can be corrected to fix, so catching it
        # here would report a server-side gap as a field error.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.project_service.create(
                scope=scope,
                name=input.name,
                description=input.description,
                state=input.state.value,
                target_date=input.target_date,
                lead_id=input.lead_id,
            )
        except ValidationError as exc:
            return ProjectPayload(project=None, errors=_errors(exc))

        return ProjectPayload(project=ProjectType.from_entity(entity, scope), errors=[])

    @strawberry.mutation
    async def project_update(
        self,
        info: Info,
        input: ProjectUpdateInput,
    ) -> ProjectPayload:
        scope = await authorized_scope(info, input.workspace_slug)

        # `state` is patched before it is unwrapped, because the three cases
        # are three different values: UNSET stays UNSET, an explicit null
        # stays None (which the service rejects, since the column is NOT
        # NULL), and a real member becomes the string the column stores.
        state = _patch(input.state)

        try:
            entity = await info.context.project_service.update(
                scope=scope,
                project_id=input.id,
                name=_patch(input.name),
                description=_patch(input.description),
                state=state.value if isinstance(state, ProjectStateType) else state,
                target_date=_patch(input.target_date),
                lead_id=_patch(input.lead_id),
            )
        except ValidationError as exc:
            return ProjectPayload(project=None, errors=_errors(exc))

        return ProjectPayload(project=ProjectType.from_entity(entity, scope), errors=[])

    @strawberry.mutation
    async def project_delete(
        self,
        info: Info,
        input: ProjectDeleteInput,
    ) -> ProjectDeletePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            await info.context.project_service.delete(
                scope=scope,
                project_id=input.id,
            )
        except ValidationError as exc:
            return ProjectDeletePayload(deleted_project_id=None, errors=_errors(exc))

        return ProjectDeletePayload(deleted_project_id=input.id, errors=[])

    @strawberry.mutation
    async def project_team_add(
        self,
        info: Info,
        input: ProjectTeamInput,
    ) -> ProjectPayload:
        """Associate a team with a project. The headline capability.

        Called once per team, so a project spanning three teams is three of
        these. A cross-workspace pair is refused by PostgreSQL rather than by
        anything in this process; the service turns the refusal into a
        NOT_FOUND on the field that named the offending id.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.project_service.add_team(
                scope=scope,
                project_id=input.project_id,
                team_id=input.team_id,
            )
        except ValidationError as exc:
            return ProjectPayload(project=None, errors=_errors(exc))

        return ProjectPayload(project=ProjectType.from_entity(entity, scope), errors=[])

    @strawberry.mutation
    async def project_team_remove(
        self,
        info: Info,
        input: ProjectTeamInput,
    ) -> ProjectPayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.project_service.remove_team(
                scope=scope,
                project_id=input.project_id,
                team_id=input.team_id,
            )
        except ValidationError as exc:
            return ProjectPayload(project=None, errors=_errors(exc))

        return ProjectPayload(project=ProjectType.from_entity(entity, scope), errors=[])

    @strawberry.mutation
    async def project_milestone_create(
        self,
        info: Info,
        input: ProjectMilestoneCreateInput,
    ) -> ProjectMilestonePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.project_service.create_milestone(
                scope=scope,
                project_id=input.project_id,
                name=input.name,
                target_date=input.target_date,
            )
        except ValidationError as exc:
            return ProjectMilestonePayload(milestone=None, errors=_errors(exc))

        return ProjectMilestonePayload(
            milestone=ProjectMilestoneType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def project_milestone_update(
        self,
        info: Info,
        input: ProjectMilestoneUpdateInput,
    ) -> ProjectMilestonePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.project_service.update_milestone(
                scope=scope,
                milestone_id=input.id,
                name=_patch(input.name),
                target_date=_patch(input.target_date),
                position=_patch(input.position),
            )
        except ValidationError as exc:
            return ProjectMilestonePayload(milestone=None, errors=_errors(exc))

        return ProjectMilestonePayload(
            milestone=ProjectMilestoneType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def project_milestone_delete(
        self,
        info: Info,
        input: ProjectMilestoneDeleteInput,
    ) -> ProjectMilestoneDeletePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            await info.context.project_service.delete_milestone(
                scope=scope,
                milestone_id=input.id,
            )
        except ValidationError as exc:
            return ProjectMilestoneDeletePayload(
                deleted_milestone_id=None,
                errors=_errors(exc),
            )

        return ProjectMilestoneDeletePayload(
            deleted_milestone_id=input.id,
            errors=[],
        )

    @strawberry.mutation
    async def issue_set_project(
        self,
        info: Info,
        input: IssueSetProjectInput,
    ) -> IssueSetProjectPayload:
        """Place an issue in a project and milestone, or take it out of both.

        On IssueService rather than ProjectService, because the statement it
        performs writes `issues` -- and the repository that owns a table is
        the one that writes it. The mutation is declared here, with the rest
        of the projects API, because that is where a client looks for it.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.issue_service.set_project(
                scope=scope,
                issue_id=input.issue_id,
                project_id=input.project_id,
                milestone_id=input.milestone_id,
            )
        except ValidationError as exc:
            return IssueSetProjectPayload(issue=None, errors=_errors(exc))

        return IssueSetProjectPayload(
            issue=IssueType.from_entity(entity, scope),
            errors=[],
        )
