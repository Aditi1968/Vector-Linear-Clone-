import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.config import get_settings
from app.db import connect, disconnect, get_pool
from app.graphql.router import build_graphql_router
from app.graphql.schema import build_schema
from app.http_limits import add_request_body_limit
from app.repositories.events import EventRepository
from app.repositories.slack import SlackRepository
from app.rest.github import router as github_router
from app.rest.health import router as health_router
from app.rest.slack import router as slack_router
from app.services.notifications import SlackNotifier, run_delivery_loop
from app.services.passwords import warm_password_hashing
from app.services.slack import DatabaseTokenStore, SlackWebClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await connect()

    # Builds the decoy hash the log-in path verifies against when no account
    # matches the submitted address. It costs one argon2 hash, and paying for
    # it here is the point: otherwise the first unknown-address log-in of the
    # process pays for two hashes where a wrong-password log-in pays for one,
    # which is exactly the timing difference the decoy exists to erase.
    await warm_password_hashing()

    delivery = asyncio.create_task(run_delivery_loop(_build_notifier()))

    try:
        yield
    finally:
        # Cancelled before the pool goes, and awaited rather than merely
        # cancelled. A task still mid-query when `disconnect()` runs would be
        # holding a connection the pool is trying to close, which surfaces as
        # an error on shutdown for work nobody is waiting for. `suppress` is
        # for the CancelledError the loop re-raises on its way out, which is
        # this frame's own cancellation arriving as expected rather than a
        # failure.
        delivery.cancel()

        with suppress(asyncio.CancelledError):
            await delivery

        await disconnect()


def _build_notifier() -> SlackNotifier:
    """Compose the Slack delivery adapter the background loop drains through.

    Built unconditionally, including on a deployment with no Slack app at all,
    and that is deliberate rather than an oversight. The events are written by
    every write path whether or not Slack exists, so a loop that only ran when
    Slack was configured would leave `domain_events` accumulating pending rows
    forever on every deployment that never connects it -- and
    `domain_events_pending_idx` growing with them. Running always means those
    rows are closed as SKIPPED within seconds and the index stays empty, which
    is the honest record: nothing was sent, and nothing was going to be.

    It also means connecting Slack does not replay history into a brand new
    channel. Everything that happened before the connection is already closed;
    the first message a channel receives is about something that happened after
    somebody chose it.

    The pool is owned by the lifespan above; this only borrows it, exactly as
    `app.graphql.context.get_context` and `app.rest.github.build_services` do.
    """
    settings = get_settings()

    return SlackNotifier(
        pool=get_pool(),
        events=EventRepository(),
        # The repository and not `SlackService`: every method on that class
        # takes an `AuthorizedWorkspaceScope`, correctly, because they are
        # things an admin does -- and this loop runs on no request and has no
        # admin to build one from.
        slack=SlackRepository(),
        token_store=DatabaseTokenStore(),
        web=SlackWebClient(),
        base_url=settings.public_base_url,
    )


def create_app() -> FastAPI:
    """Compose the application.

    This is the composition root, and the one place that resolves settings
    eagerly: a deployment missing DATABASE_URL or ENVIRONMENT should fail
    here, at startup, rather than on the first request. Everything below
    takes what it needs as an argument.

    Deliberately not called at import time. A module-level `app =
    create_app()` made importing this module resolve settings and build an
    application as a side effect, so a test or a linter that merely
    imported it failed for want of a runtime environment it never used --
    and made the composition root itself unimportable, and so untestable,
    on any machine it was supposed to fail on. Servers name the factory:
    `uvicorn app.main:create_app --factory`.
    """
    settings = get_settings()

    app = FastAPI(
        title="Vector",
        lifespan=lifespan,
    )

    add_request_body_limit(app)

    app.include_router(
        build_graphql_router(
            build_schema(settings.environment),
            settings.environment,
        ),
        prefix="/graphql",
    )
    app.include_router(health_router)

    # Mounted unconditionally, including on a deployment with no GitHub App.
    # Gating the mount on configuration would make an unconfigured deployment
    # answer 404 from the router and a configured one 404 from the handler,
    # which are the same answer arrived at two ways -- and the second is the
    # one that stays right when the settings change without a redeploy.
    app.include_router(github_router)
    # And again under `/integrations`, which is the prefix the deployment's
    # GitHub App and its Smee relay are configured against
    # (`/integrations/github/webhook`). Two paths, one router, one set of
    # handlers -- so a delivery is verified identically whichever it arrives
    # on, and neither path is a second implementation that can drift. The
    # older prefix stays because a provider's registered URL is a thing this
    # repository does not own: breaking it is a live integration outage that
    # no test here would catch.
    app.include_router(github_router, prefix="/integrations")
    # Mounted unconditionally, including on a deployment with no Slack
    # credentials. The routes exist and answer 503 "not configured" rather
    # than 404, because a mount that depended on configuration would make a
    # misconfigured deployment indistinguishable from one where the code was
    # never deployed -- and would mean the routing table differs between
    # environments, which is the thing that makes a staging test meaningless.
    app.include_router(slack_router)
    # The same dual mount, for the same reason: the Slack app's registered
    # redirect URL is configured provider-side and this process cannot read
    # it, so both spellings resolve rather than one of them 404ing an OAuth
    # callback that has already left the user's browser.
    app.include_router(slack_router, prefix="/integrations")

    return app
