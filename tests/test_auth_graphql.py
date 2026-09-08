"""The auth API over the real HTTP stack, with a fake service behind it.

Everything asserted here is about the boundary rather than the rules: what a
payload contains, what a Set-Cookie header says, and whether the token in
that header can also be found somewhere it must never be. The rules
themselves are `test_auth_service.py`'s subject, and the service is faked
here so that a failing assertion means the transport is wrong and not that a
validation message moved.

Driven through httpx over ASGI rather than `Schema.execute`, because the
cookie is the point. `execute` never builds a response, so it cannot show
that Strawberry merged the header, that the attributes survived the JSON
encoder, or that the browser would send it back -- and every one of those is
somewhere authentication could silently stop working.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from app.config import Environment, get_settings
from app.domain.auth import (
    Authentication,
    IssuedSession,
    SessionEntity,
    UserEntity,
)
from app.domain.errors import AuthenticationError, ValidationError, ValidationIssue
from app.graphql.context import get_context
from app.http_cookies import SESSION_COOKIE_NAME
from app.main import create_app

from tests.conftest import graphql_context
from tests.test_settings import PLACEHOLDER_DSN, use_environment


EMAIL = "ada@example.com"
PASSWORD = "correct horse battery staple"
NAME = "Ada Lovelace"

# Recognisable in a haystack: every assertion that the token did not leak
# searches whole response bodies for this string.
TOKEN = "session-token-3f9c1a7e-never-in-a-body"

SESSION_LIFETIME = timedelta(days=14)

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

REGISTER = """
mutation Register($input: RegisterInput!) {
  register(input: $input) {
    user { id email name }
    errors { field code message }
  }
}
"""

LOGIN = """
mutation Login($input: LoginInput!) {
  login(input: $input) {
    user { id email name }
    errors { field code message }
  }
}
"""

LOGOUT = """
mutation Logout {
  logout {
    signedOut
    errors { field code message }
  }
}
"""

ME = """
query Me {
  me { id email name }
}
"""


def make_user() -> UserEntity:
    return UserEntity(
        id=uuid4(),
        email=EMAIL,
        name=NAME,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def make_authentication(user: UserEntity) -> Authentication:
    return Authentication(
        user=user,
        issued=IssuedSession(
            token=TOKEN,
            session=SessionEntity(
                id=uuid4(),
                user_id=user.id,
                created_at=BASE_TIME,
                last_used_at=None,
                expires_at=BASE_TIME + SESSION_LIFETIME,
            ),
        ),
    )


class FakeAuthService:
    """Answers whatever a test tells it to, and records what it was asked.

    `viewers` maps a raw token to the user it identifies, which is exactly
    the interface `VectorContext.viewer` consumes -- so a request carrying a
    cookie really does have to get that cookie back out of the header for
    `me` to answer.
    """

    def __init__(self, user: UserEntity | None = None):
        self.session_lifetime = SESSION_LIFETIME
        self.user = user if user is not None else make_user()

        self.viewers: dict[str, UserEntity] = {}
        self.logged_out: list[str | None] = []
        self.register_calls: list[tuple] = []
        self.login_calls: list[tuple] = []

        # What the next call should do instead of succeeding.
        self.register_error: Exception | None = None
        self.login_error: Exception | None = None

    async def register(self, *, email, password, name, client_ip) -> Authentication:
        self.register_calls.append((email, password, name, client_ip))

        if self.register_error is not None:
            raise self.register_error

        return make_authentication(self.user)

    async def log_in(self, *, email, password, client_ip) -> Authentication:
        self.login_calls.append((email, password, client_ip))

        if self.login_error is not None:
            raise self.login_error

        return make_authentication(self.user)

    async def log_out(self, token: str | None) -> None:
        self.logged_out.append(token)
        self.viewers.pop(token, None)

    async def authenticate(self, token: str | None) -> UserEntity | None:
        if token is None:
            return None

        return self.viewers.get(token)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Never let a test's synthetic environment outlive it."""
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


@pytest.fixture
def auth_service() -> FakeAuthService:
    return FakeAuthService()


def build_client(
    auth_service: FakeAuthService,
    monkeypatch,
    tmp_path,
    environment: Environment = "test",
) -> httpx.AsyncClient:
    """The real application, with the pool and the auth service replaced.

    ASGITransport does not run the lifespan, so nothing here reaches
    PostgreSQL and the argon2 warm-up never runs either.
    """
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT=environment,
    )

    application = create_app()
    # Through the helper rather than by hand: every slot VectorContext grows
    # is filled in one place, and a resolver that reaches a service these
    # tests never wired up fails loudly instead of on a None.
    application.dependency_overrides[get_context] = lambda: graphql_context(
        auth_service=auth_service,
        environment=environment,
    )

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://vector.test",
    )


async def graphql(client: httpx.AsyncClient, query: str, **variables):
    return await client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
    )


def session_cookie(response: httpx.Response) -> str | None:
    """The raw Set-Cookie line for the session cookie, if there is one."""
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{SESSION_COOKIE_NAME}="):
            return header

    return None


def attributes(header: str) -> set[str]:
    return {part.strip().lower() for part in header.split(";")[1:]}


# --- registration ------------------------------------------------------


async def test_registering_returns_the_user_and_sets_a_session_cookie(
    auth_service, monkeypatch, tmp_path
):
    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        response = await graphql(
            client,
            REGISTER,
            input={"email": EMAIL, "password": PASSWORD, "name": NAME},
        )

    payload = response.json()["data"]["register"]

    assert payload["errors"] == []
    assert payload["user"]["email"] == EMAIL
    assert payload["user"]["name"] == NAME

    header = session_cookie(response)

    assert header is not None
    assert header.startswith(f"{SESSION_COOKIE_NAME}={TOKEN};")


async def test_the_session_token_never_appears_in_a_response_body(
    auth_service, monkeypatch, tmp_path
):
    """The header is the only place it may be.

    A token in the JSON body is a token in every client-side log, cache and
    error reporter that touches a response -- and one that JavaScript can
    read, which is the entire thing HttpOnly exists to prevent.
    """
    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        registered = await graphql(
            client,
            REGISTER,
            input={"email": EMAIL, "password": PASSWORD, "name": NAME},
        )
        logged_in = await graphql(
            client,
            LOGIN,
            input={"email": EMAIL, "password": PASSWORD},
        )

    assert TOKEN not in registered.text
    assert TOKEN not in logged_in.text


async def test_a_registration_error_sets_no_cookie(auth_service, monkeypatch, tmp_path):
    """A failed registration must not sign anybody in.

    Returning the errors *and* a Set-Cookie header would leave the client
    holding a session for an account that was never created.
    """
    auth_service.register_error = ValidationError(
        [
            ValidationIssue(
                field="email",
                code="EMAIL_TAKEN",
                message="An account with this email already exists",
            )
        ]
    )

    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        response = await graphql(
            client,
            REGISTER,
            input={"email": EMAIL, "password": PASSWORD, "name": None},
        )

    payload = response.json()["data"]["register"]

    assert payload["user"] is None
    assert payload["errors"] == [
        {
            "field": "email",
            "code": "EMAIL_TAKEN",
            "message": "An account with this email already exists",
        }
    ]
    assert session_cookie(response) is None


# --- the cookie's attributes -------------------------------------------


async def test_the_cookie_is_httponly_lax_and_rooted(
    auth_service, monkeypatch, tmp_path
):
    """The three attributes that decide what an attacker can do with it.

    HttpOnly keeps it out of `document.cookie`, so a cross-site scripting
    bug reads the page rather than walking away with the session. SameSite
    withholds it from cross-site POSTs, which is what makes a CSRF token
    unnecessary for a first-party application. Path=/ is what makes it
    present on every route that will ever need to know who is calling.
    """
    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        response = await graphql(
            client,
            LOGIN,
            input={"email": EMAIL, "password": PASSWORD},
        )

    header = session_cookie(response)

    assert header is not None
    assert "httponly" in attributes(header)
    assert "samesite=lax" in attributes(header)
    assert "path=/" in attributes(header)


async def test_the_cookie_carries_the_session_lifetime(
    auth_service, monkeypatch, tmp_path
):
    """Max-Age rather than Expires; see set_session_cookie.

    A hint to the browser, not the rule: the server decides whether a token
    still works by reading `sessions.expires_at`.
    """
    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        response = await graphql(
            client,
            LOGIN,
            input={"email": EMAIL, "password": PASSWORD},
        )

    header = session_cookie(response)

    assert header is not None
    assert f"max-age={int(SESSION_LIFETIME.total_seconds())}" in attributes(header)


async def test_the_cookie_is_secure_in_production(auth_service, monkeypatch, tmp_path):
    async with build_client(
        auth_service, monkeypatch, tmp_path, environment="production"
    ) as client:
        response = await graphql(
            client,
            LOGIN,
            input={"email": EMAIL, "password": PASSWORD},
        )

    header = session_cookie(response)

    assert header is not None
    assert "secure" in attributes(header)


@pytest.mark.parametrize("environment", ["development", "test"])
async def test_the_cookie_is_not_secure_off_production(
    auth_service, monkeypatch, tmp_path, environment
):
    """Not a relaxation -- a Secure cookie over http is one the browser drops.

    Setting it everywhere would read as caution and behave as authentication
    that silently does not work on a developer's machine.
    """
    async with build_client(
        auth_service, monkeypatch, tmp_path, environment=environment
    ) as client:
        response = await graphql(
            client,
            LOGIN,
            input={"email": EMAIL, "password": PASSWORD},
        )

    header = session_cookie(response)

    assert header is not None
    assert "secure" not in attributes(header)


# --- log-in failure ----------------------------------------------------


async def test_a_failed_log_in_names_neither_the_email_nor_the_password(
    auth_service, monkeypatch, tmp_path
):
    """The enumeration guarantee, as a client would see it.

    One code, one message, and a field that is neither of the two inputs. A
    payload that said which half was wrong would let anyone ask, one address
    at a time, who has an account here.
    """
    auth_service.login_error = AuthenticationError()

    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        response = await graphql(
            client,
            LOGIN,
            input={"email": EMAIL, "password": "wrong"},
        )

    payload = response.json()["data"]["login"]

    assert payload["user"] is None
    assert payload["errors"] == [
        {
            "field": "credentials",
            "code": "INVALID_CREDENTIALS",
            "message": "Email or password is incorrect",
        }
    ]

    body = response.text.lower()

    assert "no such" not in body
    assert "not found" not in body
    assert session_cookie(response) is None


async def test_a_failed_log_in_is_not_a_graphql_error(
    auth_service, monkeypatch, tmp_path
):
    """A wrong password is an expected answer, not a fault.

    Raised as a GraphQL error it would be masked to "Internal server error"
    by the schema's own hardening -- which is correct behaviour for a fault
    and useless as a log-in form.
    """
    auth_service.login_error = AuthenticationError()

    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        response = await graphql(
            client,
            LOGIN,
            input={"email": EMAIL, "password": "wrong"},
        )

    assert "errors" not in response.json()


# --- who am I ----------------------------------------------------------


async def test_me_is_null_without_a_cookie(auth_service, monkeypatch, tmp_path):
    """Null rather than an error; see AuthQuery.me.

    Every client asks this on load, including one that has never signed in.
    """
    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        response = await graphql(client, ME)

    assert response.json() == {"data": {"me": None}}


async def test_me_answers_from_the_cookie_the_log_in_set(
    auth_service, monkeypatch, tmp_path
):
    """The round trip, end to end.

    The cookie httpx stores comes from the log-in response's own header, and
    the token the next request presents is read back out of it by the
    context. Nothing in between is arranged by hand, so this fails if the
    header is written under a path or a name the browser would not send
    back.
    """
    auth_service.viewers[TOKEN] = auth_service.user

    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        await graphql(client, LOGIN, input={"email": EMAIL, "password": PASSWORD})

        assert client.cookies.get(SESSION_COOKIE_NAME) == TOKEN

        response = await graphql(client, ME)

    assert response.json()["data"]["me"]["email"] == EMAIL


async def test_me_is_null_for_a_token_the_service_does_not_know(
    auth_service, monkeypatch, tmp_path
):
    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        client.cookies.set(SESSION_COOKIE_NAME, "a token nobody ever issued")

        response = await graphql(client, ME)

    assert response.json() == {"data": {"me": None}}


async def test_me_never_returns_a_password_or_a_token(
    auth_service, monkeypatch, tmp_path
):
    """The User type has no field that could carry either.

    Asserted against the schema rather than against one response, because a
    field added later would leak on every query and not just this one.
    """
    auth_service.viewers[TOKEN] = auth_service.user

    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        client.cookies.set(SESSION_COOKIE_NAME, TOKEN)

        response = await graphql(client, ME)

    assert set(response.json()["data"]["me"]) == {"id", "email", "name"}
    assert TOKEN not in response.text


# --- logging out -------------------------------------------------------


async def test_logging_out_clears_the_cookie(auth_service, monkeypatch, tmp_path):
    """Cleared with the attributes it was written with, or not cleared at all.

    A browser matches a deletion against name, path and domain; a mismatch
    leaves the original cookie in place and the client keeps sending a token
    on every request.
    """
    auth_service.viewers[TOKEN] = auth_service.user

    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        await graphql(client, LOGIN, input={"email": EMAIL, "password": PASSWORD})

        assert client.cookies.get(SESSION_COOKIE_NAME) == TOKEN

        response = await graphql(client, LOGOUT)

        assert client.cookies.get(SESSION_COOKIE_NAME) is None

    header = session_cookie(response)

    assert header is not None
    assert "path=/" in attributes(header)
    assert "max-age=0" in attributes(header)


async def test_logging_out_hands_the_service_the_token_it_was_given(
    auth_service, monkeypatch, tmp_path
):
    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        client.cookies.set(SESSION_COOKIE_NAME, TOKEN)

        response = await graphql(client, LOGOUT)

    assert auth_service.logged_out == [TOKEN]
    assert response.json()["data"]["logout"] == {"signedOut": True, "errors": []}


async def test_logging_out_without_a_session_succeeds(
    auth_service, monkeypatch, tmp_path
):
    """A caller with no cookie is asking for a state they are already in.

    Reporting that nothing was deleted would confirm to whoever presented a
    token whether it was ever real.
    """
    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        response = await graphql(client, LOGOUT)

    assert auth_service.logged_out == [None]
    assert response.json()["data"]["logout"] == {"signedOut": True, "errors": []}


async def test_me_is_null_after_logging_out(auth_service, monkeypatch, tmp_path):
    auth_service.viewers[TOKEN] = auth_service.user

    async with build_client(auth_service, monkeypatch, tmp_path) as client:
        await graphql(client, LOGIN, input={"email": EMAIL, "password": PASSWORD})
        await graphql(client, LOGOUT)

        response = await graphql(client, ME)

    assert response.json() == {"data": {"me": None}}
