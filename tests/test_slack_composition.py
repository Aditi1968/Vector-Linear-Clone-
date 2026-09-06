"""Slack is optional, and this is the file that proves it. No database.

The hard requirement behind the whole feature: a deployment that has never
heard of Slack must boot, serve every request and pass every gate with none of
the three settings present. Everything else about the integration is worthless
if adding it made Slack a prerequisite for running Vector.

The complement is here too -- that the routes and the schema fields exist
anyway, unconditionally -- because a feature that disappears when it is
unconfigured is one whose absence and whose breakage look identical.
"""

import pytest
from pydantic import SecretStr

from app.config import Settings, get_settings
from app.graphql.schema import MUTATION_TYPES, QUERY_TYPES, build_schema
from app.main import create_app
from app.rest.slack import router as slack_router

from tests.test_settings import PLACEHOLDER_DSN, use_environment


SLACK_ROUTE_PATHS = {
    "/slack/oauth/start",
    "/slack/oauth/callback",
    "/slack/events",
}


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Never let a test's synthetic environment outlive it."""
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


def settings_with(**slack: str) -> Settings:
    """Settings built from explicit values, never from the environment.

    Constructed with keywords rather than through `get_settings()` because
    this suite is about the fields themselves, and because a Settings resolved
    from the process would read whatever the developer's shell or `.env`
    happens to hold -- which is the one thing a test about optionality must
    not depend on.
    """
    return Settings(
        database_url=SecretStr(PLACEHOLDER_DSN),
        environment="test",
        **slack,
    )


# --- the settings themselves ------------------------------------------


def test_a_deployment_with_no_slack_settings_is_unconfigured():
    settings = settings_with()

    assert settings.slack_client_id is None
    assert settings.slack_client_secret is None
    assert settings.slack_signing_secret is None
    assert settings.slack_configured is False


def test_all_three_settings_make_a_configured_deployment():
    assert (
        settings_with(
            slack_client_id="123.456",
            slack_client_secret=SecretStr("shh"),
            slack_signing_secret=SecretStr("also-shh"),
        ).slack_configured
        is True
    )


@pytest.mark.parametrize(
    "present",
    [
        {"slack_client_id": "123.456"},
        {"slack_client_secret": SecretStr("shh")},
        {"slack_signing_secret": SecretStr("also-shh")},
        {"slack_client_id": "123.456", "slack_client_secret": SecretStr("shh")},
    ],
    ids=["client id", "client secret", "signing secret", "id and secret"],
)
def test_a_partial_configuration_is_not_a_configuration(present):
    """Half a Slack app cannot do anything, so it is not "configured".

    An OAuth start with no client secret produces a callback that cannot
    exchange its code; an events endpoint with no signing secret cannot verify
    a single request. Reporting either as configured would advertise a connect
    button that dead-ends.
    """
    assert settings_with(**present).slack_configured is False


def test_the_two_credentials_are_secrets_and_the_client_id_is_not():
    """SecretStr keeps a value out of a repr, a traceback and a log line.

    The client id is deliberately plain: it travels in the authorize URL a
    browser is redirected to, so wrapping it would be theatre. The other two
    are bearer credentials, and the asymmetry is the point.
    """
    settings = settings_with(
        slack_client_id="123.456",
        slack_client_secret=SecretStr("shh"),
        slack_signing_secret=SecretStr("also-shh"),
    )

    printed = repr(settings)

    assert "shh" not in printed
    assert "also-shh" not in printed
    assert settings.slack_client_secret.get_secret_value() == "shh"


# --- the application boots without them --------------------------------


def test_the_application_is_created_with_no_slack_configuration(monkeypatch, tmp_path):
    """The requirement, stated as a test.

    `use_environment` clears every variable Settings knows how to read --
    derived from `model_fields`, so the three Slack ones are included -- and
    chdirs away from the repository's `.env`. What is left is a process with a
    database URL, an environment, and no Slack app at all.
    """
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="test",
    )

    application = create_app()

    assert get_settings().slack_configured is False

    # Read off the OpenAPI document rather than by walking `app.routes`. The
    # route list holds mounts and included-router wrappers whose internals are
    # FastAPI's business and have changed shape between versions; the
    # generated paths are the documented surface and say the same thing.
    #
    # Mounted anyway. A router mounted only when configured would make a
    # deployment that forgot the credentials indistinguishable from one
    # running a build without the feature.
    assert SLACK_ROUTE_PATHS <= set(application.openapi()["paths"])


def test_the_router_declares_exactly_the_three_documented_routes():
    """Pinned, so a fourth REST route is a line a reviewer reads.

    CLAUDE.md reserves REST for OAuth callbacks and webhooks. A product
    endpoint arriving here would be a layering decision made by accident, and
    this is the cheapest place to notice it.
    """
    assert {route.path for route in slack_router.routes} == SLACK_ROUTE_PATHS


# --- the schema carries the fields either way --------------------------


def test_the_slack_root_fields_reach_the_schema():
    """Both root fields are actually on the root types.

    `merge_types` builds Query and Mutation from the tuples in
    app/graphql/schema.py, and a feature that forgot to extend one of them
    would leave a resolver module that imports cleanly and a schema that has
    never heard of it.
    """
    sdl = build_schema("test").as_str()

    assert "slackIntegration(workspaceSlug: String!): SlackIntegration!" in sdl
    assert "slackDisconnect(input: SlackDisconnectInput!): SlackDisconnectPayload!" in (
        sdl
    )


def test_the_slack_types_are_registered_once_each():
    """One entry per root tuple, so a merge cannot silently double a field.

    `merge_types` warns on a name collision rather than raising, and a warning
    in a test run is a line nobody reads.
    """
    from app.graphql.mutations.slack import SlackMutation
    from app.graphql.queries.slack import SlackQuery

    assert QUERY_TYPES.count(SlackQuery) == 1
    assert MUTATION_TYPES.count(SlackMutation) == 1
