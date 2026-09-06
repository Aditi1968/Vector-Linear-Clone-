import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.issue import IssueCreateInput
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueCreatePayload, IssueType


@strawberry.type
class Mutation:
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

        try:
            entity = await info.context.issue_service.create(
                scope=scope,
                team_id=team_id,
                title=input.title,
                description=input.description,
                priority=input.priority,
            )
        except ValidationError as exc:
            # Only expected input validation is translated into the payload.
            # Everything else (asyncpg failures, bugs, outages) propagates
            # through GraphQL's normal error mechanism.
            return IssueCreatePayload(
                issue=None,
                errors=[ValidationErrorType.from_domain(issue) for issue in exc.issues],
            )

        return IssueCreatePayload(
            issue=IssueType.from_entity(entity),
            errors=[],
        )
