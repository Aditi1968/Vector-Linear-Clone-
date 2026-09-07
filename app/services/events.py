"""Emitting domain events, from inside the transaction that caused them.

This module is the ONLY thing a write path needs in order to feed the
notification pipeline, and what it deliberately does not import is the point:
there is no `app.services.slack` here, no `app.domain.slack`, and no HTTP
client. A service that changes an issue, a service that publishes a project
update and the GitHub webhook handler all call a function here and are done.
Which workspaces want the event, which channel it goes to, whether Slack is
even connected, and what happens when Slack says no are decided later, by
`app.services.notifications`, reading the rows these functions write.

That separation is the feature and not an architectural preference. A webhook
handler that could reach the Slack client is a handler somebody will eventually
make post directly from -- and then the preference toggle does not apply to it,
the redelivery posts twice, the failure is invisible, and the network call
happens while a pool connection and an open transaction are held. Every one of
those is a bug that cannot be written from here, because from here Slack does
not exist.

Functions rather than a collaborator object every writing service has to be
constructed with, for the reason `app.services.activity` sets out at length:
the alternative was a required constructor argument on `IssueService`,
`GithubService`, `ProjectService`, `TriageService` and `BulkService` for an
object that holds no pool, no configuration and no state. They take the
CALLER'S connection for the same reason that module's write half does -- an
event written outside the transaction that caused it is either an announcement
of something that rolled back, or a change nobody was told about.
"""

from uuid import UUID, uuid4

import asyncpg

from app.domain.events import DomainEventKind
from app.domain.tenancy import WorkspaceScope
from app.repositories.events import EventRepository


# How much of a project update's body reaches a channel.
#
# A first line rather than a paragraph. The message is one line by design --
# see `message_for` -- and a channel that reproduces a weekly update in full is
# one people scroll past, which is the same as not sending it.
UPDATE_SUMMARY_MAX_LENGTH = 140

# What a project update with an empty-looking body reads as.
#
# `domain_events_summary_length` requires at least one character, and a body
# that begins with a blank line has an empty first one. A message saying
# something ordinary beats a delivery that fails a CHECK on a project update
# somebody legitimately wrote.
UPDATE_SUMMARY_FALLBACK = "Posted an update"

# The kinds whose event only exists when the issue is now in a completed state.
#
# A set rather than an `if kind is ISSUE_COMPLETED`, so the rule reads as a
# property of the vocabulary and a second completion-shaped kind does not need
# this function reopened.
_COMPLETION_KINDS = frozenset({DomainEventKind.ISSUE_COMPLETED})

# Stateless, so one is all this process needs. It holds no connection, no pool
# and no scope -- every one of those arrives per call -- which is what makes a
# module-level instance a shared function table rather than shared state. The
# same judgement `app.services.activity` makes about its three.
_events = EventRepository()


def _fresh_key() -> str:
    """A dedupe key for a fact that can honestly happen more than once.

    An issue assigned to Ana, then to Ben, then to Ana again is three events.
    A key derived from (issue, assignee) would swallow the third forever, and
    one derived from (issue, kind) would swallow every assignment after the
    first -- so for these paths the honest answer to "what would make two
    emissions the same event" is "nothing, they are different events".

    Nothing is lost by that, because the exactly-once guarantee is already
    somewhere better: these functions run inside the transaction that performs
    the change, so the change and its event commit together or not at all. A
    retried mutation is a second change and deserves a second event.

    The paths that genuinely need suppression -- a provider that reports one
    fact many times -- build a NATURAL key instead. See
    `EventRepository.record_pull_request_merged`.
    """
    return uuid4().hex


async def record_issue_event(
    connection: asyncpg.Connection,
    *,
    scope: WorkspaceScope,
    kind: DomainEventKind,
    issue_id: UUID,
) -> None:
    """Emit one issue event, on the caller's connection.

    Deliberately silent about whether a row was written. The caller is
    mid-write and has nothing to do with the answer: an issue whose state moved
    to something that is not a completed state produces no row, and that is an
    ordinary outcome rather than a failure of the write that caused it.

    The completion test is not made here and is not made by the caller. It is a
    join inside the statement, so "what counts as completed" is decided once,
    by `workflow_states.type`, rather than by every service that moves an
    issue -- and it costs no extra round trip to decide.
    """
    await _events.record_about_issue(
        connection,
        scope=scope,
        kind=kind,
        dedupe_key=_fresh_key(),
        issue_id=issue_id,
        only_when_completed=kind in _COMPLETION_KINDS,
    )


async def record_pull_request_merged(
    connection: asyncpg.Connection,
    *,
    scope: WorkspaceScope,
    repository_id: int,
    number: int,
    title: str,
) -> None:
    """Emit that a pull request merged, on the caller's connection.

    Called from the GitHub webhook path, inside the one transaction that
    applies a delivery -- so an event exists exactly when the merge it
    describes was stored, and a delivery that failed part way through leaves
    neither.

    A thin pass-through, and worth existing anyway: it is what lets
    `app.services.github` emit an event without holding a repository of its
    own, and -- more to the point -- what lets that module reach the pipeline
    through an import that has no Slack anywhere beneath it.

    The dedupe key is built inside the repository, from the repository id, the
    pull request number and the issue. That is a fact GitHub will report many
    times: it retries a delivery it did not see a 2xx for, and it also sends
    the whole `pull_request` object on every later EDIT of an already-merged
    pull request, under a delivery id nothing has seen before.
    """
    await _events.record_pull_request_merged(
        connection,
        scope=scope,
        repository_id=repository_id,
        number=number,
        title=title,
    )


async def record_project_update(
    connection: asyncpg.Connection,
    *,
    scope: WorkspaceScope,
    project_id: UUID,
    health: str,
    body: str,
) -> None:
    """Emit what a project update is worth announcing, on the caller's connection.

    Up to two events for one update, because they answer different questions
    and a workspace toggles them separately: `project_health_changed` is "this
    project's status moved", which a lead acts on; `project_update_published`
    is "there is a new written update", which a team reads. A workspace that
    wants only the first has said so in `slack_notification_preferences`, and
    collapsing them here would mean choosing on its behalf.

    ORDER MATTERS, and this must be called before `projects.health` is stamped
    with the new value. The health event's statement compares against the
    STORED health to decide whether anything moved, so running it after the
    update would compare the new value with itself and announce nothing, ever.
    `ProjectService.post_update` calls this between its two writes for exactly
    that reason.
    """
    await _events.record_about_project(
        connection,
        scope=scope,
        kind=DomainEventKind.PROJECT_HEALTH_CHANGED,
        dedupe_key=_fresh_key(),
        project_id=project_id,
        summary=f"Now {health.replace('_', ' ')}",
        only_when_health_moves_to=health,
    )

    await _events.record_about_project(
        connection,
        scope=scope,
        kind=DomainEventKind.PROJECT_UPDATE_PUBLISHED,
        dedupe_key=_fresh_key(),
        project_id=project_id,
        summary=_update_summary(body),
    )


def _update_summary(body: str) -> str:
    """The first line of an update, bounded, and never empty.

    `splitlines()` rather than `split("\\n")` so a body written on Windows does
    not arrive with a trailing carriage return in the middle of a Slack
    message.
    """
    first = next((line.strip() for line in body.splitlines() if line.strip()), "")

    return first[:UPDATE_SUMMARY_MAX_LENGTH] or UPDATE_SUMMARY_FALLBACK
