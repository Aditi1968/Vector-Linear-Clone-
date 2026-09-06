from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.types.label import LabelConnection, LabelType


DEFAULT_FIRST = 50


@strawberry.type
class LabelQuery:
    """The read half of labels, merged into the root Query by app.graphql.schema.

    A separate class rather than more fields on the issues query, for the
    reason `merge_types` is used at all: the root is a junction every feature
    hangs off, and a junction that is also one file is a file every branch
    conflicts in.
    """

    @strawberry.field
    async def label(self, info: Info, id: UUID) -> LabelType | None:
        # The workspace comes from the request, never from the document.
        # app/graphql/tenancy.py holds where it comes from today and what
        # replaces that. A label in another workspace resolves to null -- the
        # same answer as an id that exists nowhere -- so the resolver cannot
        # be used to ask whether someone else's label exists.
        scope = await info.context.tenant.scope()

        entity = await info.context.label_service.get_by_id(scope=scope, label_id=id)

        if entity is None:
            return None

        return LabelType.from_entity(entity)

    @strawberry.field
    async def labels(
        self,
        info: Info,
        first: int = DEFAULT_FIRST,
        after: str | None = None,
    ) -> LabelConnection:
        scope = await info.context.tenant.scope()

        try:
            page = await info.context.label_service.list(
                scope=scope,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected pagination input errors are translated. Anything
            # else (asyncpg failures, bugs) propagates as a real execution
            # error. `from None` keeps parser detail out of the response.
            raise GraphQLError(
                "Invalid pagination arguments",
                extensions={
                    "code": "BAD_USER_INPUT",
                    "issues": [
                        {
                            "field": issue.field,
                            "code": issue.code,
                            "message": issue.message,
                        }
                        for issue in exc.issues
                    ],
                },
            ) from None

        return LabelConnection.from_domain(page)
