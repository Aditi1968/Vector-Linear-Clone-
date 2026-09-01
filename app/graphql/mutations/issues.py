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
        try:
            entity = await info.context.issue_service.create(
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
                errors=[
                    ValidationErrorType.from_domain(issue) for issue in exc.issues
                ],
            )

        return IssueCreatePayload(
            issue=IssueType.from_entity(entity),
            errors=[],
        )
