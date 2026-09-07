import strawberry
from strawberry.types import Info
from strawberry.types.unset import UnsetType as StrawberryUnsetType

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.patch import UNSET
from app.graphql.inputs.release import (
    EnvironmentCreateInput,
    ReleaseCreateInput,
    ReleaseDeleteInput,
    ReleaseStatusSetInput,
)
from app.graphql.scope import authorized_scope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.release import (
    EnvironmentPayload,
    EnvironmentType,
    ReleaseDeletePayload,
    ReleasePayload,
    ReleaseType,
)


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


_BAD_REPOSITORY_ID = ValidationIssue(
    field="repositoryId",
    code="INVALID",
    message="Repository id must be a positive integer",
)


def _repository_id(raw: str) -> int:
    """Parse the ID a client sent into the BIGINT the column holds.

    `Release.repositoryId` crosses the wire as an ID because GitHub's ids are
    64-bit and a GraphQL Int is not, so the value arrives as a string that
    Strawberry has not checked the shape of. `int(...)` on it raises
    ValueError, which is an unhandled exception in a resolver and reaches the
    client as a masked "Internal server error" -- for input a client could
    trivially correct. Translated into a field error instead.

    The sign is checked here as well as by
    `github_repositories_repository_id_positive`, because a negative id would
    otherwise reach the database, fail the foreign key, and be reported as
    "Repository not found" -- true, but not the sentence that tells a client
    what it did wrong.
    """
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError([_BAD_REPOSITORY_ID]) from None

    if value <= 0:
        raise ValidationError([_BAD_REPOSITORY_ID])

    return value


@strawberry.type
class ReleaseMutation:
    """The write half of the releases API.

    Every resolver here has the same three lines of shape: resolve the tenant,
    call one service method, translate a ValidationError into the payload.
    Nothing decides anything -- what a legal status move is, which commits a
    release spans, what its notes say -- because all of that is in
    ReleaseService, where REST and workers reach it too.

    `except ValidationError` and nothing broader. An asyncpg failure, a bug or
    an outage propagates through GraphQL's normal error mechanism and is masked
    by app/graphql/schema.py; converting it into a field error would tell a
    client to fix input that was never the problem, behind a 200.
    """

    @strawberry.mutation
    async def environment_create(
        self,
        info: Info,
        input: EnvironmentCreateInput,
    ) -> EnvironmentPayload:
        # Resolved before the try, and outside it. A missing workspace is not
        # something the client's input can be corrected to fix, so catching it
        # here would report a server-side gap as a field error.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.release_service.create_environment(
                scope=scope,
                name=input.name,
                kind=input.kind.value,
            )
        except ValidationError as exc:
            return EnvironmentPayload(environment=None, errors=_errors(exc))

        return EnvironmentPayload(
            environment=EnvironmentType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def release_create(
        self,
        info: Info,
        input: ReleaseCreateInput,
    ) -> ReleasePayload:
        """Cut a release, resolving and freezing its notes in one transaction.

        The headline capability. Everything the note says is derived from what
        migration 017 already recorded about this repository -- no AI, no
        second source, and nothing a client may assert.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        # Translated here rather than in the service, because the two
        # sentinels belong to different layers: `strawberry.UNSET` is a
        # Strawberry object and services must not import Strawberry (CLAUDE.md),
        # exactly as `Info` stops here. `isinstance` and not `is`: identical at
        # runtime, but the field is annotated `str | None` while carrying an
        # `UnsetType`, and an identity test between two types mypy believes
        # cannot overlap is reported as a mistake under `strict_equality`.
        requested_previous = input.previous_commit_sha
        previous_commit_sha = (
            UNSET
            if isinstance(requested_previous, StrawberryUnsetType)
            else requested_previous
        )

        try:
            entity = await info.context.release_service.create(
                scope=scope,
                name=input.name,
                environment_id=input.environment_id,
                repository_id=_repository_id(input.repository_id),
                commit_sha=input.commit_sha,
                previous_commit_sha=previous_commit_sha,
            )
        except ValidationError as exc:
            return ReleasePayload(release=None, errors=_errors(exc))

        return ReleasePayload(release=ReleaseType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def release_status_set(
        self,
        info: Info,
        input: ReleaseStatusSetInput,
    ) -> ReleasePayload:
        """Move a release to a new status, if the move is legal from where it
        is. An illegal one comes back as an INVALID_TRANSITION field error
        naming the state the release is actually in."""
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.release_service.set_status(
                scope=scope,
                release_id=input.id,
                status=input.status.value,
            )
        except ValidationError as exc:
            return ReleasePayload(release=None, errors=_errors(exc))

        return ReleasePayload(release=ReleaseType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def release_delete(
        self,
        info: Info,
        input: ReleaseDeleteInput,
    ) -> ReleaseDeletePayload:
        """Delete a release and the record of what it shipped.

        Present as much for the RESTRICT chain as for the product:
        `releases_repository_fk` refuses to let a workspace disconnect its
        GitHub integration while releases name its repositories, and this is
        the way out of that.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            await info.context.release_service.delete(
                scope=scope,
                release_id=input.id,
            )
        except ValidationError as exc:
            return ReleaseDeletePayload(deleted_release_id=None, errors=_errors(exc))

        return ReleaseDeletePayload(deleted_release_id=input.id, errors=[])
