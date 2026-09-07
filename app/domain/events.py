"""What happened, in the vocabulary an integration announces.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL, and no
import of `app.domain.slack`. That absence is the point rather than tidiness:
the services that EMIT events -- issues, projects, the GitHub webhook -- import
this module, and if it reached into the Slack domain then every one of them
would transitively depend on Slack. A write path that can see the delivery
adapter is a write path somebody will eventually make post directly from, which
is the coupling this whole pipeline exists to prevent.

Distinct from `app.domain.activity` and `app.domain.notifications`, and the
three are not layers of one thing. An activity row is what the system recorded
happening to an issue. A notification is what ONE PERSON still has to look at.
A domain event is what a WORKSPACE may want announced somewhere outside Vector
-- and the three vocabularies are deliberately different lengths, because the
history records everything, the inbox records what interrupts somebody, and a
channel a whole team reads records only what a team would act on within the
hour.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID


class DomainEventKind(StrEnum):
    """The six events worth announcing outside Vector.

    A StrEnum because the member IS the stored spelling, so nothing converts at
    the repository boundary and a kind cannot reach the database in a casing
    `domain_events_kind_known` refuses.

    Spelled identically to `SLACK_NOTIFICATION_EVENTS` in `app.domain.slack`,
    and that equality is load-bearing rather than a coincidence: it makes
    "does this workspace want this announced" a lookup of `(workspace_id,
    kind)` in `slack_notification_preferences`, with nothing translating
    between two vocabularies. A translation table is a third place these six
    names are written down and the one that fails silently -- an event whose
    toggle nobody can find, because the toggle is spelled differently from the
    thing it controls. `tests/test_notification_pipeline.py` pins the two
    equal.

    The two modules still do not import each other. This one is what a write
    path may see; that one is what the delivery adapter reads. Sharing a tuple
    between them would mean an issue service importing the Slack domain to say
    that an issue was assigned.
    """

    ISSUE_ASSIGNED = "issue_assigned"
    ISSUE_COMPLETED = "issue_completed"
    ISSUE_PRIORITY_URGENT = "issue_priority_urgent"
    PROJECT_HEALTH_CHANGED = "project_health_changed"
    PROJECT_UPDATE_PUBLISHED = "project_update_published"
    PULL_REQUEST_MERGED = "pull_request_merged"


# The `priority` value that means urgent.
#
# `issues.priority` is `CHECK (priority BETWEEN 0 AND 4)` in migration 001,
# where 0 is "no priority" rather than the lowest one -- see
# `IssueOrderField.PRIORITY` in app/domain/issues.py, which orders by
# `NULLIF(priority, 0)` for exactly that reason. 1 is the top of the scale.
#
# Named here rather than spelled as a literal at the one call site, because a
# bare `== 1` in a notification rule is the kind of line that gets read as a
# count.
URGENT_PRIORITY: Final = 1


class DeliveryState(StrEnum):
    """Every state a Slack delivery can be in.

    The application's copy of `domain_events_slack_state_known`, for the reason
    `ActivityKind` gives about keeping one: the database has to refuse an
    unknown state whoever writes it, and this code has to know the vocabulary
    without asking the database.

    PENDING and DELIVERED are self-explanatory. The pair worth being exact
    about is the other two:

    * FAILED  -- something went wrong and it will not be retried. There is a
      reason, and somebody may have to act on it.
    * SKIPPED -- nothing went wrong and nothing was sent. The workspace has the
      event switched off, has not connected Slack, or has chosen no channel.
      Terminal, and NOT an error -- counting it as one is how a healthy
      integration comes to look broken on a screen that tallies failures.
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"


# Why a delivery did not happen, beyond the six `SLACK_FAILURES` already names.
#
# These two are decisions this pipeline makes rather than answers Slack gave,
# which is why they live here and not in the Slack domain's vocabulary:
#
# * PREFERENCE_DISABLED -- the workspace has this event switched off. Absence of
#   a preference row means off (migration 018 argues for opt-in), so this is by
#   far the commonest reason a skipped row exists.
# * NO_INSTALLATION -- nobody ever connected Slack here. Deliberately distinct
#   from SLACK_FAILURES' `not_connected`, which is what a token that has STOPPED
#   working reports: one means "reconnect", the other means "there is nothing
#   wrong".
PREFERENCE_DISABLED: Final = "preference_disabled"
NO_INSTALLATION: Final = "no_installation"


# What each kind reads as at the front of a message.
#
# A phrase and not a sentence, because the message is one line and the subject
# follows it. Written here rather than in the delivery service so that the
# words are next to the vocabulary they name, and so that a kind added to the
# enum without a headline is a KeyError in a test rather than a message that
# renders as "None".
HEADLINES: Final[dict[DomainEventKind, str]] = {
    DomainEventKind.ISSUE_ASSIGNED: "Assigned",
    DomainEventKind.ISSUE_COMPLETED: "Completed",
    DomainEventKind.ISSUE_PRIORITY_URGENT: "Now urgent",
    DomainEventKind.PROJECT_HEALTH_CHANGED: "Project health",
    DomainEventKind.PROJECT_UPDATE_PUBLISHED: "Project update",
    DomainEventKind.PULL_REQUEST_MERGED: "Pull request merged",
}


@dataclass(frozen=True, slots=True)
class DomainEventEntity:
    """One event, as the delivery path reads it back.

    Carries everything a message needs and nothing else -- in particular no
    recipient, because Slack delivery is to a workspace channel and the
    per-person inbox is `notifications`. There is no field here through which a
    caller could name who hears about something.

    `attempts` is how many times delivery has been CLAIMED, this one included,
    so it is at least 1 by the time anything holds one of these. The delivery
    service compares it against a ceiling to decide whether a transient failure
    gets another go.
    """

    workspace_id: UUID
    kind: DomainEventKind
    dedupe_key: str
    subject: str
    summary: str
    path: str
    attempts: int


def message_for(event: DomainEventEntity, *, base_url: str | None) -> str:
    """The one line this event reads as in a channel, plus its link.

    Compact on purpose. A Slack channel that renders a paragraph per event is
    a channel muted within a week, which is the same as no integration arrived
    at expensively -- the same argument migration 018 makes for the vocabulary
    being six items long.

    The link is a deep link to the real Vector route (`/acme/issues/<id>`),
    built by joining the deployment's origin to the path the event stored. The
    two halves are stored apart because only one of them is a property of the
    event: a path recorded in March is still right after the deployment moves
    to a new hostname, and an absolute URL recorded in March is not.

    A deployment that has not configured an origin gets the line WITHOUT a
    link, rather than a guessed one. There is no host this process can infer
    that is not a guess -- a request's `Host` header is whatever a proxy set,
    and this runs on no request at all -- and a message linking to the wrong
    place is worse than one linking nowhere.

    Nothing here is escaped, and nothing needs to be: `path` is built by the
    emitting statements out of a workspace slug, a team key, an issue number
    and a uuid, all of them values this schema already constrains.
    """
    line = f"{HEADLINES[event.kind]} · {event.subject} — {event.summary}"

    if base_url is None:
        return line

    return f"{line}\n{base_url}{event.path}"
