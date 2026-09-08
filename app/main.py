import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.db import connect, disconnect, get_pool
from app.graphql.router import build_graphql_router
from app.graphql.schema import build_schema
from app.http_limits import add_request_body_limit
from app.repositories.embedding_jobs import EmbeddingJobRepository
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.events import EventRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.issues import IssueRepository
from app.repositories.label_groups import LabelGroupRepository
from app.repositories.labels import LabelRepository
from app.repositories.notifications import NotificationRepository
from app.repositories.recurrences import RecurrenceRepository
from app.repositories.reminders import DueReminderRepository
from app.repositories.slack import SlackRepository
from app.repositories.teams import TeamRepository
from app.repositories.templates import TemplateRepository
from app.rest.github import router as github_router
from app.rest.health import router as health_router
from app.rest.slack import router as slack_router
from app.services.embedding_jobs import EmbeddingWorker
from app.services.embeddings import load_embedder
from app.services.issues import IssueService
from app.services.labels import LabelService
from app.services.notifications import SlackNotifier, run_delivery_loop
from app.services.passwords import warm_password_hashing
from app.services.schedule import ScheduleWorker
from app.services.slack import DatabaseTokenStore, SlackWebClient
from app.services.teams import TeamService
from app.services.templates import TemplateService


def _start_embedding_worker(settings: Settings) -> "asyncio.Task[None] | None":
    """The embedding worker, if this deployment asked for one. THE SCHEDULER.

    There isn't one, and this is the honest replacement rather than a stand-in
    for a missing dependency. Semantic search needs `issue_embeddings` filled
    in; migration 025 builds every read over it and PostgreSQL has no model, so
    something in application space has to run the embedder on a timer. The
    options were a cron container, a queue broker, or a task on the process that
    is already running -- and the first two are infrastructure a deployment has
    to operate, bought to call a coroutine every thirty seconds.

    So: an asyncio task, behind one boolean. `EMBEDDING_WORKER_ENABLED=true` on
    a deployment of this image makes that process drain the queue as well as
    serve requests. Setting it on more than one is safe and intended -- the
    claim is `FOR UPDATE SKIP LOCKED`, so N workers split the backlog instead of
    duplicating it -- and setting it on a deployment with no traffic routed to
    it is how model work is kept off the request path without a second image, a
    second entrypoint, or a second thing to keep in step with this one.

    Returns None when the flag is off, which is the whole of the "off" path:
    nothing is constructed, no embedder is loaded, and the process behaves
    exactly as it did before this existed.

    Built here rather than in `app.graphql.context`, because that function runs
    per REQUEST and this object is per PROCESS. The pool is `get_pool()` and not
    a new one: the worker releases its connection across the model run precisely
    so that sharing the request path's pool is safe.
    """
    if not settings.embedding_worker_enabled:
        return None

    worker = EmbeddingWorker(
        pool=get_pool(),
        jobs=EmbeddingJobRepository(),
        embeddings=EmbeddingRepository(),
        # Never fails and never blocks startup on a model that is not there:
        # `load_embedder` falls back to the in-tree hashing embedder when no
        # sentence-transformers install is present, so a deployment with the
        # flag on and no model still indexes -- with a weaker embedder that
        # names itself differently, which re-queues every vector by itself the
        # day a real model is installed.
        embedder=load_embedder(),
    )

    return asyncio.create_task(worker.run_forever())


def _build_schedule_worker() -> ScheduleWorker:
    """Compose the pass that turns dates into notifications and issues.

    Built unconditionally and with no flag in front of it, unlike the embedding
    worker and like `run_delivery_loop`. The distinction is what the work IS:
    the embedding worker does MODEL work, which is expensive and which a
    deployment may reasonably decline, so it is opt-in. This is the only thing
    that makes a due date do anything and the only thing that files a recurring
    issue -- a deployment that ran it on no process would have two product
    features that silently never happen, and nothing anywhere reporting it.

    Running it on SEVERAL processes is safe and needs no coordination, which is
    the point of both claim mechanisms: reminders claim by writing a row whose
    primary key a second replica collides with, recurrences claim with
    `FOR UPDATE SKIP LOCKED` and an advance in the same transaction. So the two
    replicas `k8s/30-api.yaml` runs share the calendar rather than doubling it.

    Composed here rather than borrowed from `app.graphql.context.get_context`,
    which runs per REQUEST and builds a whole context this loop has no use for.
    The services below are the request path's own -- the sweep files issues
    through the same `TemplateService` a person does, so every rule an apply
    respects applies to a scheduled issue too. The pool is `get_pool()` and not
    a new one, exactly as `_build_notifier` borrows it.
    """
    pool = get_pool()

    team_service = TeamService(pool=pool, repository=TeamRepository())

    return ScheduleWorker(
        pool=pool,
        reminders=DueReminderRepository(),
        # The repository and not `ActivityService`: every notification method
        # on that class takes an `AuthorizedWorkspaceScope`, correctly, because
        # they are things a person does to their own inbox -- and this loop
        # runs on no request and has no person to build one from. The same
        # reach `_build_notifier` makes for `SlackRepository`.
        notifications=NotificationRepository(),
        recurrences=RecurrenceRepository(),
        templates=TemplateService(
            pool=pool,
            repository=TemplateRepository(),
            recurrences=RecurrenceRepository(),
            issues=IssueService(
                pool=pool,
                repository=IssueRepository(),
                teams=team_service,
            ),
            labels=LabelService(
                pool=pool,
                repository=LabelRepository(),
                issue_label_repository=IssueLabelRepository(),
                group_repository=LabelGroupRepository(),
            ),
        ),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await connect()

    # Builds the decoy hash the log-in path verifies against when no account
    # matches the submitted address. It costs one argon2 hash, and paying for
    # it here is the point: otherwise the first unknown-address log-in of the
    # process pays for two hashes where a wrong-password log-in pays for one,
    # which is exactly the timing difference the decoy exists to erase.
    await warm_password_hashing()

    # Both after `connect()`, because both take the pool; the embedding flag
    # is read from the same cached settings `connect()` resolved.
    #
    # Three background tasks, started and stopped independently. The embedding
    # worker is opt-in because it does model work; the delivery loop always
    # runs, because `domain_events` is written by every write path whether or
    # not Slack exists and those rows have to be closed either way -- see
    # `_build_notifier`. The schedule sweep always runs for a stronger reason:
    # it is the only thing that makes a due date do anything and the only thing
    # that files a recurring issue, so a deployment without it has two features
    # that silently never happen.
    worker = _start_embedding_worker(get_settings())
    delivery = asyncio.create_task(run_delivery_loop(_build_notifier()))
    schedule = asyncio.create_task(_build_schedule_worker().run_forever())

    try:
        yield
    finally:
        # Cancelled before the pool goes and awaited rather than merely
        # cancelled, for the reason spelled out at `delivery` below: a task
        # still mid-statement when `disconnect()` runs holds a connection the
        # pool is trying to close.
        schedule.cancel()

        with suppress(asyncio.CancelledError):
            await schedule

        if worker is not None:
            worker.cancel()

            # Awaited rather than merely cancelled, and before `disconnect()`.
            # `cancel()` only schedules the cancellation; returning without
            # awaiting would close the pool underneath a task still inside a
            # statement, which surfaces as an InterfaceError logged during
            # shutdown from a coroutine nobody is watching.
            #
            # The CancelledError is the task acknowledging the cancel we asked
            # for, so suppressing it here is reading the reply rather than
            # swallowing a failure -- `run_forever` lets it through untouched
            # for exactly this.
            with suppress(asyncio.CancelledError):
                await worker

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
