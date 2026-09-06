import strawberry
from starlette.responses import Response
from strawberry.types import Info

from app.domain.auth import Authentication
from app.domain.errors import AuthenticationError, ValidationError, ValidationIssue
from app.graphql.inputs.auth import LoginInput, RegisterInput
from app.graphql.types.auth import (
    LoginPayload,
    LogoutPayload,
    RegisterPayload,
    UserType,
)
from app.graphql.types.errors import ValidationErrorType
from app.http_cookies import clear_session_cookie, set_session_cookie


# What a failed log-in reports. The field is neither "email" nor "password":
# a client that could tell which half was wrong could ask, one address at a
# time, which addresses have accounts here. One code, one message, one field,
# for every way a log-in can fail.
INVALID_CREDENTIALS = ValidationIssue(
    field="credentials",
    code="INVALID_CREDENTIALS",
    message="Email or password is incorrect",
)


@strawberry.type
class AuthMutation:
    @strawberry.mutation
    async def register(self, info: Info, input: RegisterInput) -> RegisterPayload:
        try:
            authentication = await info.context.auth_service.register(
                email=input.email,
                password=input.password,
                name=input.name,
            )
        except ValidationError as exc:
            # Only expected input validation is translated into the payload.
            # Everything else (asyncpg failures, bugs, outages) propagates
            # through GraphQL's normal error mechanism and is masked there.
            return RegisterPayload(
                user=None,
                errors=[ValidationErrorType.from_domain(issue) for issue in exc.issues],
            )

        return RegisterPayload(
            user=_sign_in(info, authentication),
            errors=[],
        )

    @strawberry.mutation
    async def login(self, info: Info, input: LoginInput) -> LoginPayload:
        try:
            authentication = await info.context.auth_service.log_in(
                email=input.email,
                password=input.password,
            )
        except AuthenticationError:
            # Deliberately not distinguished from any other failure, and
            # deliberately raised as a payload entry rather than a GraphQL
            # error: a wrong password is an expected answer to a well-formed
            # request, not a fault.
            return LoginPayload(
                user=None,
                errors=[ValidationErrorType.from_domain(INVALID_CREDENTIALS)],
            )

        return LoginPayload(
            user=_sign_in(info, authentication),
            errors=[],
        )

    @strawberry.mutation
    async def logout(self, info: Info) -> LogoutPayload:
        """End the caller's session and clear their cookie.

        The server-side delete happens first and the cookie is cleared after.
        That order is the safe one: if the second step fails the browser is
        left holding a token that no longer authenticates anything, whereas
        the reverse would leave a live session behind a cookie nobody can see
        to revoke.

        Succeeds whether or not the caller had a session. See LogoutPayload.
        """
        context = info.context

        await context.auth_service.log_out(context.session_token())

        clear_session_cookie(_response(info), environment=context.environment)

        return LogoutPayload(signed_out=True, errors=[])


def _sign_in(info: Info, authentication: Authentication) -> UserType:
    """Put the issued token in a Set-Cookie header and return the user.

    This is the only place a raw session token is written to a response, and
    it writes it to a header rather than into the payload: a token in the
    JSON body is a token in every client-side log, cache and error reporter
    that touches a response, and one that JavaScript can read defeats the
    HttpOnly flag entirely.

    The token is not returned, logged, or kept. After this call the only copy
    that exists is in the browser, and the only thing the server holds is its
    digest.
    """
    context = info.context

    set_session_cookie(
        _response(info),
        token=authentication.issued.token,
        max_age=int(context.auth_service.session_lifetime.total_seconds()),
        environment=context.environment,
    )

    return UserType.from_entity(authentication.user)


def _response(info: Info) -> Response:
    """The response strawberry will merge headers from.

    Raising when it is absent, rather than skipping the cookie, is the whole
    point of the helper. A register or log-in that reported success without a
    Set-Cookie header would tell a client it was signed in while leaving it
    with no way to prove it -- and would leave a live session in the database
    that nobody can present and nothing will revoke. An internal error is the
    honest report of that, and it is masked on the way out like any other.
    """
    # Annotated because `Info.context` is untyped: without it the attribute
    # is Any and this function would satisfy its own return type by saying
    # nothing at all.
    response: Response | None = info.context.response

    if response is None:
        raise RuntimeError("GraphQL context carries no response to set a cookie on")

    return response
