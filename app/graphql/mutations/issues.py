from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError, ValidationIssue
from app.graphql.inputs.issue import IssueCreateInput, IssueUpdateInput
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import (
    IssueArchivePayload,
    IssueCreatePayload,
    IssueType,
    IssueUpdatePayload,
)
from app.graphql.viewer import actor_user_id


# The one answer every operation on an issue that is not there must give.
#
# "No such issue", "an issue in another workspace", and "an issue that has
# been archived" all produce exactly this, byte for byte. The distinctions are
# real on the server and must not be observable: a caller holding a guessed id
# who could tell them apart would have a way to enumerate other tenants' issue
# ids, one request at a time, without ever reading a row.
#
# It travels in `errors` rather than as a null `issue` with an empty list,
# because a payload with neither an issue nor an error says only that nothing
# happened and leaves a client to guess why.
ISSUE_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Issue not found",
)


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class Mutation:
    """The issue feature's mutations -- not the root type.

    `app.graphql.schema` merges this with every other feature's mutation
    class into the root `Mutation`. Inheriting the others into this class
    would also produce a working schema and would put this one file on the
    path of every feature that adds a mutation; worse, `merge_types` counts
    the fields it merges and warns on a collision, and a class that arrives
    by inheritance is never counted.
    """

    @strawberry.mutation
    async def issue_create(
        self,
        info: Info,
        input: IssueCreateInput,
    ) -> IssueCreatePayload:
        # Resolved before the try, and outside it. Neither call raises
        # ValidationError -- a missing workspace or an unprovisioned tenant
        # is not something the client's input can be corrected to fix -- so
        # catching them here would report a server-side gap as a field
        # error on the input the client sent.
        scope = await info.context.tenant.scope()
        team_id = await info.context.tenant.team_id(scope)

        # Authorship comes from whoever authenticated the request, and there
        # is no input field for it: a client able to name a creator could
        # forge one. None for an anonymous caller, which is a state
        # `issues.creator_id` already allows -- whether anonymous callers may
        # file at all is a question for authorisation, not for this column.
        viewer = await info.context.viewer()

        try:
            entity = await info.context.issue_service.create(
                scope=scope,
                team_id=team_id,
                title=input.title,
                description=input.description,
                priority=input.priority,
                assignee_id=input.assignee_id,
                creator_id=viewer.id if viewer is not None else None,
                estimate=input.estimate,
                due_date=input.due_date,
            )
        except ValidationError as exc:
            # Only expected input validation is translated into the payload.
            # Everything else (asyncpg failures, bugs, outages) propagates
            # through GraphQL's normal error mechanism.
            return IssueCreatePayload(issue=None, errors=_errors(exc))

        return IssueCreatePayload(
            issue=IssueType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def issue_update(
        self,
        info: Info,
        id: UUID,
        input: IssueUpdateInput,
    ) -> IssueUpdatePayload:
        """Change some of one issue's fields, leaving the rest alone.

        The workspace comes from the request, never from the document, so
        an id belonging to another tenant reaches a statement scoped to
        this one and matches no row.
        """
        scope = await info.context.tenant.scope()
        actor_id = await actor_user_id(info)

        try:
            entity = await info.context.issue_service.update(
                scope=scope,
                issue_id=id,
                patch=input.to_patch(),
                actor_id=actor_id,
            )
        except ValidationError as exc:
            return IssueUpdatePayload(issue=None, errors=_errors(exc))

        if entity is None:
            return IssueUpdatePayload(
                issue=None,
                errors=[ValidationErrorType.from_domain(ISSUE_NOT_FOUND)],
            )

        return IssueUpdatePayload(
            issue=IssueType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def issue_archive(
        self,
        info: Info,
        id: UUID,
    ) -> IssueArchivePayload:
        """Take an issue off the board, reversibly and without discarding it.

        Named for what it does. This product archives rather than deletes,
        because migration 005 never reissues an issue number and the
        identifier built from it -- ENG-42 -- is the issue's name in URLs,
        commit messages and conversation; a deleted row would turn every
        one of those references into one that resolves to nothing.
        migrations/006_issue_fields.sql carries the full argument.

        The archived issue is returned so a client can render the change
        from this result. It is the last time any query here will hand that
        row back.
        """
        scope = await info.context.tenant.scope()
        actor_id = await actor_user_id(info)

        entity = await info.context.issue_service.archive(
            scope=scope,
            issue_id=id,
            actor_id=actor_id,
        )

        if entity is None:
            return IssueArchivePayload(
                issue=None,
                errors=[ValidationErrorType.from_domain(ISSUE_NOT_FOUND)],
            )

        return IssueArchivePayload(
            issue=IssueType.from_entity(entity),
            errors=[],
        )
