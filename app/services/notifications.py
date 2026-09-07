"""The delivery half of the notification pipeline: events out to Slack.

The chain this completes is

    domain event -> notification policy -> delivery adapter -> Slack Web API

and this module is the last three links. `app.services.events` writes the first
one, from inside whatever transaction caused it, and knows nothing about any of
this.

Three properties are worth reading the code for, because each of them is a
failure this design exists to make unreachable rather than a rule somebody
remembers.

NO CONNECTION IS HELD ACROSS THE NETWORK CALL. Delivering one event takes three
short transactions -- claim, then post with nothing held, then record the
outcome -- rather than one long one. A pool exhausted by workers waiting on
Slack is an outage in the rest of the product caused by an integration, and it
is exactly what a `SELECT ... FOR UPDATE` held across `chat.postMessage` would
produce. `SlackService.sync_channels` states the same rule for the path an
admin is watching; this is the path nobody is watching, where it matters more.

A RETRY CANNOT HOT-LOOP. That is enforced in two independent places, and
neither of them is this module's loop being careful. `EventRepository.claim_next`
moves `slack_next_attempt_at` forward in the same statement that increments the
attempt counter, so a failing event is not due again until the backoff has
passed -- however many processes are draining, and even if one dies mid-attempt.
`MAX_ATTEMPTS` is the second: a transient failure that keeps recurring becomes a
recorded failure rather than an unbounded retry.

SUCCESS CANNOT BE REPORTED FOR A FAILURE. `mark_delivered` is reachable from
exactly one place -- after `post_message` returned without raising, which
happens only for a Slack response that said `ok: true` -- and
`domain_events_delivered_has_an_instant` refuses the state without its
timestamp. There is no branch here that catches an exception and records a
delivery, and no default that starts out delivered.

What this module deliberately does NOT do is choose a recipient. Slack delivery
is to a workspace CHANNEL, read from `slack_notification_settings` for the
event's own workspace; the per-person inbox stays `notifications`, written by
`app.services.activity` from recipients its own statement derives. There is no
parameter anywhere in this file through which a person could be named.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import asyncpg
import structlog

from app.domain.events import (
    NO_INSTALLATION,
    PREFERENCE_DISABLED,
    DeliveryState,
    DomainEventEntity,
    message_for,
)
from app.domain.slack import SCOPE_POST_MESSAGE, SlackApiError
from app.repositories.events import EventRepository
from app.repositories.slack import SlackRepository
from app.services.slack import (
    SlackTokenStore,
    SlackWebApi,
    _failure_for,
    fetch_token,
)


logger = structlog.get_logger(__name__)

# How long after a failed attempt an event becomes due again, multiplied by the
# attempt number up to `BACKOFF_STEPS`. So: a minute, two, three, four, five,
# and five thereafter.
#
# Linear rather than exponential, deliberately. The failure this actually meets
# is Slack being briefly unreachable or rate-limiting, which clears in seconds
# to minutes; exponential backoff is for a dependency whose recovery time is
# unknown, and here it would mean an event that failed twice waiting out an
# outage that ended half an hour ago.
RETRY_DELAY = timedelta(minutes=1)
BACKOFF_STEPS = 5

# How many attempts a transient failure gets before it is recorded as one.
#
# Counted at CLAIM time, so a process that dies between posting and recording
# has spent an attempt -- which is the honest accounting: the message may well
# have been delivered, and retrying it forever on the chance it was not is how
# a channel receives the same message a hundred times.
MAX_ATTEMPTS = 5

# How many events one drain pass will deliver before returning.
#
# A bound rather than "everything due", so that a backlog cannot turn one pass
# into a call that runs for minutes and cannot be shut down cleanly. Whatever
# is left is due immediately and the next pass takes it.
#
# ponytail: no pacing between posts within a pass, and one drainer. Together
# with POLL_INTERVAL_SECONDS this is a ceiling of four events a second, which no
# workspace approaches -- and Slack rate-limits `chat.postMessage` at roughly one
# per second per channel anyway, answering 429, which arrives here as
# SLACK_UNREACHABLE and is retried with backoff rather than lost. A deployment
# that outgrows the ceiling raises this number and runs more than one instance;
# `FOR UPDATE SKIP LOCKED` already makes that safe. A per-channel token bucket is
# the fix after that, and only then.
DRAIN_BATCH = 20

# How long the loop waits between passes when it finds nothing to do.
#
# The floor under "cannot hot-loop", independent of everything the database
# does: even a pass that raises immediately is followed by this sleep.
POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _Route:
    """Where one event's message is going, and what may present it.

    Built only when every check has passed, which is what makes it impossible
    to hold one of these and not be entitled to post. The token is a field on a
    short-lived local rather than state on any object, so nothing long-lived in
    this process has a credential on it for a traceback to print -- the same
    judgement `SlackWebApi` makes by taking the token per call.
    """

    channel_id: str
    token: str


@dataclass(frozen=True, slots=True)
class _Refusal:
    """Why an event will not be posted, and which terminal word says so.

    A type rather than a bare reason string, because the two halves must not
    drift apart: `skipped` and `failed` are the same write with different
    meanings, and a function returning only the reason would leave the caller
    to remember which of them each reason belongs to. Deciding both in the one
    place that knows why is what keeps a disabled preference from ever being
    counted as an integration error.
    """

    state: DeliveryState
    failure: str


class SlackNotifier:
    """Drains `domain_events` into a workspace's Slack channel.

    Holds a pool like every other service, and owns its own transaction
    boundaries -- three per event, for the reason the module docstring gives.

    Takes no scope on any method, and that absence needs stating rather than
    passing unnoticed: there is no viewer here. This runs on no request, on
    behalf of nobody, from a background loop. What stands in for an
    authorization check is that the workspace is never supplied -- it arrives
    on the row the database matched, and every lookup that follows (the
    preference, the channel, the token) is keyed on THAT id. There is no
    argument anywhere through which a caller could aim one workspace's event at
    another workspace's channel, which is the property migration 027's opening
    paragraph is about.

    `base_url` is passed in rather than read from settings at the point of use,
    so that a process cannot render one message with a link and the next
    without one.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        events: EventRepository,
        slack: SlackRepository,
        token_store: SlackTokenStore,
        web: SlackWebApi,
        base_url: str | None = None,
    ):
        self._pool = pool
        self._events = events

        # The Slack repository, not the Slack service. `SlackService`'s methods
        # all take an `AuthorizedWorkspaceScope` -- correctly, because they are
        # things an admin does -- and this path has no admin to build one from.
        # Reaching for the repository is the same move `ProjectService` makes
        # for `IssueRepository`: a service owning the transaction, reading a
        # table another service also reads.
        self._slack = slack

        self._token_store = token_store
        self._web = web
        self._base_url = base_url.rstrip("/") if base_url else None

    async def deliver_pending(self, *, limit: int = DRAIN_BATCH) -> int:
        """Deliver up to `limit` due events; answer how many Slack accepted.

        The count is deliveries and not attempts, so a pass that skipped
        nineteen disabled events and delivered one answers 1. A caller with a
        number that counted skips would be reading "the integration is busy"
        off a workspace that has it switched off.

        Stops early on an empty claim rather than sleeping, because there is
        nothing to wait for inside a pass -- waiting is the loop's job, and one
        place that decides how long to wait is one place to change it.
        """
        delivered = 0

        for _ in range(limit):
            event = await self._claim()

            if event is None:
                break

            if await self._deliver(event):
                delivered += 1

        return delivered

    async def _claim(self) -> DomainEventEntity | None:
        """Take one due event, in a transaction that commits immediately.

        Committing before any work is the point, and it is the argument
        `SlackService.claim_event` makes for the Slack events endpoint: a claim
        held open until the delivery finished would be invisible to a second
        drainer, which would then block on a lock rather than move on to the
        next event. Committing first makes "already claimed" a fact the other
        process reads.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await self._events.claim_next(
                    connection,
                    retry_delay=RETRY_DELAY,
                    backoff_steps=BACKOFF_STEPS,
                )

    async def _deliver(self, event: DomainEventEntity) -> bool:
        """Post one claimed event, or record why it was not posted.

        The three phases are visible in the three awaits: read the policy on
        one connection and release it, post with nothing held, then record the
        outcome on a fresh one.

        Returns whether Slack accepted the message, and returns False for every
        other outcome including the deliberate ones. A skip is not a delivery.
        """
        route = await self._route(event)

        if isinstance(route, _Refusal):
            await self._finish(event, route.state, route.failure)

            return False

        try:
            await self._web.post_message(
                token=route.token,
                channel=route.channel_id,
                text=message_for(event, base_url=self._base_url),
            )
        except SlackApiError as error:
            # The only exception caught here, and it is the one the Web API
            # seam raises for every way a Slack call can fail. A broader
            # `except Exception` would record a bug in this module as "Slack
            # refused", which is a wrong answer an operator would act on.
            await self._record_failure(event, _failure_for(error))

            return False

        # Reachable only from a Slack response that said `ok: true`, because
        # that is the only way `post_message` returns rather than raising.
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._events.mark_delivered(connection, event=event)

        return True

    async def _route(self, event: DomainEventEntity) -> _Route | _Refusal:
        """Where this event goes, or the reason it goes nowhere.

        Everything the database can answer is read here, on ONE connection
        which is then released -- the network call happens with nothing held.

        Every lookup is keyed on `event.workspace_id`, which came off the row
        the claim matched. That is the whole cross-tenant story: there is no
        second workspace in scope, so there is no way for this event to be
        routed to a channel belonging to one. Migration 018's composite foreign
        key is the floor under it -- a settings row cannot even NAME a channel
        of another tenant -- and this is what makes a mistake here unmade
        rather than merely unstorable.

        The order of the refusals is the design, because each is a different
        sentence and the earlier ones make the later ones meaningless:

        * no installation -- nobody connected Slack here. Every table in
          migration 018 hangs off `slack_installations`, so there cannot be a
          preference or a channel either, and reporting one of those would
          describe a screen this workspace has never seen.
        * the preference is off -- and ABSENCE OF A ROW IS OFF, which is 018's
          opt-in rule and the reason this reads the stored rows rather than a
          merged view. Posting into a company's Slack is a visible act; it does
          not start happening because somebody picked a channel. This comes
          before the two configuration checks below on purpose: a workspace
          that has switched an event off is owed no report about its scopes.
        * `chat:write` not granted -- an admin declined the scope individually,
          so the fix is reconnecting. Checked against
          `slack_installations.scopes`, what Slack actually agreed to, and
          never against REQUESTED_SCOPES: what this release asks for says
          nothing about what a particular workspace permitted.
        * no channel chosen -- connected, permitted, nowhere to post.

        Three of the four answer a SKIP and only the scope answers a failure,
        and that split is a product decision rather than a technicality.
        Nothing is wrong with a workspace that has an event switched off or has
        not finished setting Slack up; a scope an admin declined is a
        half-installed integration somebody has to go and fix.
        """
        workspace_id = event.workspace_id

        async with self._pool.acquire() as connection:
            installation = await self._slack.find(
                connection,
                workspace_id=workspace_id,
            )

            if installation is None:
                return _Refusal(DeliveryState.SKIPPED, NO_INSTALLATION)

            preferences = await self._slack.list_preferences(
                connection,
                workspace_id=workspace_id,
            )

            # Absence is off. `.get(..., False)` is the whole opt-in rule, and
            # it is spelled here rather than reached through
            # `SlackService._settings` -- which merges every event in the
            # vocabulary for a settings SCREEN, and would leave this path
            # depending on a presentation helper to decide whether to post.
            enabled = {
                preference.event: preference.enabled for preference in preferences
            }

            if not enabled.get(event.kind.value, False):
                return _Refusal(DeliveryState.SKIPPED, PREFERENCE_DISABLED)

            if SCOPE_POST_MESSAGE not in installation.scopes:
                return _Refusal(DeliveryState.FAILED, "missing_scope")

            chosen = await self._slack.find_default_channel(
                connection,
                workspace_id=workspace_id,
            )

            if chosen is None:
                return _Refusal(DeliveryState.SKIPPED, "no_default_channel")

            stored = await self._slack.find_token_reference(
                connection,
                workspace_id=workspace_id,
            )

        if stored is None:
            # The installation row and the token reference are columns of the
            # same row, so this is only reachable if the workspace
            # disconnected between the two reads -- which is exactly "nobody
            # has Slack connected here" by the time the message would be sent.
            return _Refusal(DeliveryState.SKIPPED, NO_INSTALLATION)

        channel_id, _ = chosen

        return _Route(
            channel_id=channel_id,
            token=await fetch_token(
                self._token_store,
                stored,
                workspace_id=workspace_id,
            ),
        )

    async def _record_failure(self, event: DomainEventEntity, failure: str) -> None:
        """Write down what Slack said, and decide whether to try again.

        One reason is transient and every other one is not.
        SLACK_UNREACHABLE means no answer arrived -- a timeout, a refused
        connection, a 429, Slack being unwell -- and that is worth another go.
        A refusal Slack actually articulated is not: an archived channel, a
        bot that has not been invited, a declined scope and a revoked token
        all stay true until a person changes something, and retrying them is
        how a dead integration spends a workspace's rate limit forever.

        Even the transient one is bounded. Past MAX_ATTEMPTS it is recorded as
        a failure carrying Slack's real reason rather than a synthetic "gave
        up" -- an operator needs to know WHAT kept failing, and "we tried five
        times" is the one fact they can already see in `slack_attempts`.

        A retryable failure that has attempts left is left PENDING and written
        nowhere. That is not a swallowed error: the claim already recorded the
        attempt and pushed the next one out, so the row says "tried once, due
        again in a minute", which is exactly true. Writing a failure reason
        onto a row that is about to be retried would make a transient blip look
        like a broken integration on every screen that reads the column, and
        `domain_events_progress_has_no_reason` refuses it outright.
        """
        if failure == "slack_unreachable" and event.attempts < MAX_ATTEMPTS:
            logger.info(
                "slack.delivery.retrying",
                workspace_id=str(event.workspace_id),
                kind=event.kind.value,
                attempts=event.attempts,
            )

            return

        await self._finish(event, DeliveryState.FAILED, failure)

    async def _finish(
        self,
        event: DomainEventEntity,
        state: DeliveryState,
        failure: str,
    ) -> None:
        """Close an event without a delivery, in a transaction of its own."""
        logger.info(
            "slack.delivery.not_sent",
            workspace_id=str(event.workspace_id),
            kind=event.kind.value,
            state=state.value,
            failure=failure,
        )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._events.mark_not_delivered(
                    connection,
                    event=event,
                    state=state,
                    failure=failure,
                )


async def run_delivery_loop(
    notifier: SlackNotifier,
    *,
    interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Drain the outbox forever, at a pace nothing can make faster.

    A polling loop rather than a task fired off by whichever request wrote the
    event, and the reason is what happens when the process dies. A
    fire-and-forget task is lost with the process that scheduled it, and the
    row it was going to deliver sits pending with nothing that will ever look
    at it again; a poller finds it on the next pass, on any instance. It is
    also the shape that lets the writing transaction commit and return without
    waiting on anything -- which is the whole reason the event is a row rather
    than a call.

    The sleep is AFTER the pass and outside the try, so it happens on every
    path including the one where `deliver_pending` raises on its first
    statement. That ordering is the loop's half of "cannot hot-loop": a
    database that refuses every connection would otherwise be a tight loop of
    failing connects with a log line each.

    Every exception except cancellation is caught and logged. A loop that dies
    on one bad row is an integration that silently stops for everybody, and
    discovering that from a user asking why Slack went quiet is discovering it
    far too late. `CancelledError` is re-raised so shutdown actually shuts
    down -- catching it is how a lifespan hangs on a task that will not stop.
    """
    while True:
        try:
            await notifier.deliver_pending()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("slack.delivery.pass_failed")

        await asyncio.sleep(interval)
