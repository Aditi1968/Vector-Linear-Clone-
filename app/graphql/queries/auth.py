import strawberry
from strawberry.types import Info

from app.graphql.types.auth import UserType


@strawberry.type
class AuthQuery:
    @strawberry.field
    async def me(self, info: Info) -> UserType | None:
        """The signed-in user, or null.

        Null rather than an UNAUTHENTICATED error, because "who am I" is not
        a protected operation -- it is the question a client asks in order to
        find out whether it may ask protected ones, and every client asks it
        on load. An error response would make the ordinary anonymous case
        indistinguishable from a failure, and would mean a page that has not
        signed in yet renders an error it has to special-case away.

        Protected fields are a different matter: those must refuse, not
        return null, so that a missing session is never mistaken for missing
        data. None exist yet.
        """
        entity = await info.context.viewer()

        if entity is None:
            return None

        return UserType.from_entity(entity)
