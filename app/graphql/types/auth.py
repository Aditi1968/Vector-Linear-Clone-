from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.auth import UserEntity
from app.graphql.types.errors import ValidationErrorType


@strawberry.type(name="User")
class UserType:
    """A user account as the API exposes one.

    There is no password field and no session field, because the entity this
    is built from carries neither. That is the guarantee, not this docstring:
    a hash cannot be added to a response here by accident, because there is
    nothing in `UserEntity` to add.

    `email` is exposed, and today the only field returning a User is `me` --
    so the only address any caller can read is their own. That stops being
    true the moment a User is reachable from somebody else's data (an issue's
    assignee, a workspace's members), and at that point the address has to
    move behind a check or off the shared type. Worth deciding then rather
    than discovering.
    """

    id: UUID
    email: str
    name: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: UserEntity) -> "UserType":
        return cls(
            id=entity.id,
            email=entity.email,
            name=entity.name,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class RegisterPayload:
    user: UserType | None
    errors: list[ValidationErrorType]


@strawberry.type
class LoginPayload:
    """The result of a log-in attempt.

    A failure fills `errors` with exactly one entry, whose field is neither
    "email" nor "password". Which of the two was wrong is the one thing this
    payload must never carry.
    """

    user: UserType | None
    errors: list[ValidationErrorType]


@strawberry.type
class LogoutPayload:
    """The result of signing out.

    `signed_out` is true whenever `errors` is empty, and `errors` is always
    empty today -- so the field looks like it says nothing. What it says is
    that the caller now has no session, which is true whether they arrived
    with a live one, an expired one, a forged one, or none at all. It is
    deliberately not "a session was deleted": answering that would tell
    whoever presented a token whether it was ever real.

    The field exists so that a later failure with something to report -- a
    rate limit, most likely -- can be added without changing the shape of a
    successful response.
    """

    signed_out: bool
    errors: list[ValidationErrorType]
