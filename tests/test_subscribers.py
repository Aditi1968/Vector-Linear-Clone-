"""Watching an issue, without a database.

Four things are checked here and nothing else -- what needs a server is in
tests/test_migration_020_db.py:

  * the statements are scoped to a workspace AND a user, and the insert
    absorbs a duplicate rather than raising, which is what makes
    auto-subscribe callable from inside somebody else's transaction;
  * a refusal from `issue_subscribers_issue_fk` becomes "Issue not found",
    and any other constraint stays an error -- the rule CLAUDE.md states as
    "do not convert unexpected database errors into validation errors";
  * participation subscribes: a new assignee is watching before anyone is
    notified, and a status move notifies;
  * no field in the schema lets a caller name whose subscription it is.

The last is the security property of the whole feature and it is testable
without a database precisely because it is an absence.
"""

import re
from types import SimpleNamespace
from uuid import UUID

import asyncpg
import pytest
import strawberry

from app.domain.activity import IssueSnapshot
from app.domain.errors import ValidationError, WorkspaceAccessDeniedError
from app.domain.notifications import NotificationKind
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.graphql.schema import build_schema
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE
from app.repositories.subscribers import SubscriberRepository
from app.services import activity
from app.services.activity import ActivityService

from tests.conftest import (
    FakeConnection,
    FakePool,
    graphql_context,
    normalize,
)


WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a2")
ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
VIEWER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OTHER_USER_ID = UUID("00000000-0000-7000-8000-0000000000e2")
STATE_ONE = UUID("00000000-0000-7000-8000-0000000000c1")
STATE_TWO = UUID("00000000-0000-7000-8000-0000000000c2")

SCOPE = AuthorizedWorkspaceScope(
    workspace_id=WORKSPACE_ID,
    user_id=VIEWER_ID,
    role="member",
)

schema = build_schema("test")


def snapshot(**overrides) -> IssueSnapshot:
    fields = {
        "title": "Original",
        "priority": 1,
        "workflow_state_id": STATE_ONE,
        "assignee_id": None,
        "project_id": None,
        "cycle_id": None,
    }

    return IssueSnapshot(**(fields | overrides))


def service(connection: FakeConnection) -> tuple[ActivityService, FakePool]:
    """An ActivityService over a fake pool, with real repositories.

    Real repositories deliberately: the statements are the subject of half
    this file, and a fake repository would assert that the service called
    something rather than that the something was scoped.
    """
    pool = FakePool(connection)

    return (
        ActivityService(
            pool=pool,
            repository=SimpleNamespace(),
            notifications=SimpleNamespace(),
            subscribers=SubscriberRepository(),
        ),
        pool,
    )


# --- the statements ---------------------------------------------------


async def test_subscribing_is_scoped_to_a_workspace_an_issue_and_a_user():
    """Three equalities and no fourth parameter.

    The workspace comes from the scope and the user from the scope's
    `user_id`, so there is no argument through which either could be
    something the client sent.
    """
    connection = FakeConnection(value=VIEWER_ID)

    await SubscriberRepository().subscribe(
        connection,
        scope=SCOPE,
        issue_id=ISSUE_ID,
        user_id=VIEWER_ID,
    )

    assert connection.queries[0]["args"] == (WORKSPACE_ID, ISSUE_ID, VIEWER_ID)


async def test_subscribing_twice_is_absorbed_rather_than_raised():
    """The property that makes auto-subscribe safe inside another
    transaction.

    A constraint violation in PostgreSQL aborts the whole transaction, so a
    second comment on one issue must not reach the primary key at all --
    "catch it and carry on" is not available to a caller that is mid-write.
    """
    connection = FakeConnection(value=None)

    started = await SubscriberRepository().subscribe(
        connection,
        scope=SCOPE,
        issue_id=ISSUE_ID,
        user_id=VIEWER_ID,
    )

    text = normalize(connection.queries[0]["query"])

    assert "ON CONFLICT ON CONSTRAINT issue_subscribers_pkey DO NOTHING" in text
    assert started is False, "a duplicate did not start anything"


async def test_unsubscribing_names_the_workspace_in_the_predicate():
    """Not a check applied to a row already read.

    A DELETE that matched on the issue and user alone and then compared the
    workspace would have destroyed another tenant's row before discovering
    it was not this caller's.
    """
    connection = FakeConnection(value=None)

    went = await SubscriberRepository().unsubscribe(
        connection,
        scope=SCOPE,
        issue_id=ISSUE_ID,
        user_id=VIEWER_ID,
    )

    text = normalize(connection.queries[0]["query"])

    assert "WHERE workspace_id = $1 AND issue_id = $2 AND user_id = $3" in text
    assert went is False


async def test_the_watcher_list_is_ordered_totally():
    """(created_at, user_id), because two auto-subscribes in one transaction
    share a timestamp to the microsecond -- and an order that is not total
    makes one list come back two ways on two reads."""
    connection = FakeConnection(rows=[])

    await SubscriberRepository().list_for_issue(
        connection,
        scope=SCOPE,
        issue_id=ISSUE_ID,
        limit=10,
    )

    assert "ORDER BY created_at, user_id" in normalize(connection.queries[0]["query"])


# --- which refusals are the client's to fix ---------------------------


class RefusingConnection(FakeConnection):
    """A connection whose next statement violates one named constraint."""

    def __init__(self, constraint: str):
        super().__init__()

        self._constraint = constraint

    async def fetchval(self, query, *args):
        error = asyncpg.ForeignKeyViolationError("refused")
        error.constraint_name = self._constraint

        raise error


async def test_subscribing_to_an_issue_that_is_not_here_is_a_field_error():
    """One answer for "another workspace's issue" and "no such issue".

    Telling them apart would let a caller holding a guessed id learn that the
    issue is real and simply not theirs.
    """
    subject, _ = service(RefusingConnection("issue_subscribers_issue_fk"))

    with pytest.raises(ValidationError) as raised:
        await subject.subscribe(scope=SCOPE, issue_id=ISSUE_ID)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("issueId", "NOT_FOUND")
    ]


async def test_another_constraint_is_not_translated_into_a_field_error():
    """CLAUDE.md's rule, asserted rather than assumed.

    `issue_subscribers_user_fk` cannot fire for an AuthorizedWorkspaceScope --
    that scope is built from a membership row -- so a violation of it means
    the row went away mid-request. That is a failure to surface and mask, not
    advice for a client to act on.
    """
    subject, _ = service(RefusingConnection("issue_subscribers_user_fk"))

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await subject.subscribe(scope=SCOPE, issue_id=ISSUE_ID)


# --- participation subscribes -----------------------------------------


class RecordingActivity:
    """Stands in for the three module-level writers `record_changes` calls."""

    def __init__(self):
        self.recorded: list[dict] = []
        self.notified: list[dict] = []
        self.subscribed: list[dict] = []


@pytest.fixture
def writes(monkeypatch) -> RecordingActivity:
    calls = RecordingActivity()

    async def record(connection, **kwargs):
        calls.recorded.append(kwargs)

    async def notify(connection, **kwargs):
        calls.notified.append(kwargs)

    async def auto_subscribe(connection, **kwargs):
        calls.subscribed.append(kwargs)

    monkeypatch.setattr(activity, "record", record)
    monkeypatch.setattr(activity, "notify", notify)
    monkeypatch.setattr(activity, "auto_subscribe", auto_subscribe)

    return calls


async def test_a_new_assignee_is_subscribed_before_anybody_is_notified(writes):
    """Order is the assertion, not decoration.

    The fan-out statement reads `issue_subscribers`, so a subscription written
    afterwards would be a subscription the very notification it exists for did
    not see. Being handed an issue is the clearest statement that its future
    concerns you, so the assignee is watching from that moment.
    """
    await activity.record_changes(
        FakeConnection(),
        scope=SCOPE,
        issue_id=ISSUE_ID,
        actor_id=VIEWER_ID,
        before=snapshot(),
        after=snapshot(assignee_id=OTHER_USER_ID),
    )

    assert writes.subscribed == [
        {"scope": SCOPE, "issue_id": ISSUE_ID, "user_id": OTHER_USER_ID}
    ]
    assert [call["kind"] for call in writes.notified] == [NotificationKind.ASSIGNED]


async def test_reassigning_to_the_same_person_subscribes_nobody(writes):
    """`changes` has already dropped the non-move, and this is the half of
    that rule the notification path relies on: a write that touches the
    assignee column without moving it is not an event."""
    await activity.record_changes(
        FakeConnection(),
        scope=SCOPE,
        issue_id=ISSUE_ID,
        actor_id=VIEWER_ID,
        before=snapshot(assignee_id=OTHER_USER_ID),
        after=snapshot(assignee_id=OTHER_USER_ID),
    )

    assert writes.subscribed == []
    assert writes.notified == []


async def test_unassigning_notifies_nobody_and_subscribes_nobody(writes):
    """There is nobody to tell. The history still records it -- `changes`
    produces the row -- and the inbox does not."""
    await activity.record_changes(
        FakeConnection(),
        scope=SCOPE,
        issue_id=ISSUE_ID,
        actor_id=VIEWER_ID,
        before=snapshot(assignee_id=OTHER_USER_ID),
        after=snapshot(),
    )

    assert writes.subscribed == []
    assert writes.notified == []
    assert len(writes.recorded) == 1


async def test_a_status_move_notifies_the_watchers(writes):
    """The event subscribers exist for.

    An assignee can see the status on the issue they own; a watcher asked to
    follow it precisely so they would not have to open it. `include_creator`
    is left at its default, so every issue's author does not hear about every
    move.
    """
    await activity.record_changes(
        FakeConnection(),
        scope=SCOPE,
        issue_id=ISSUE_ID,
        actor_id=VIEWER_ID,
        before=snapshot(),
        after=snapshot(workflow_state_id=STATE_TWO),
    )

    assert [call["kind"] for call in writes.notified] == [
        NotificationKind.STATUS_CHANGED
    ]
    assert "include_creator" not in writes.notified[0]


async def test_moving_the_state_and_the_assignee_produces_two_notifications(writes):
    """Two things happened, and collapsing them would mean choosing which one
    to hide."""
    await activity.record_changes(
        FakeConnection(),
        scope=SCOPE,
        issue_id=ISSUE_ID,
        actor_id=VIEWER_ID,
        before=snapshot(),
        after=snapshot(assignee_id=OTHER_USER_ID, workflow_state_id=STATE_TWO),
    )

    assert [call["kind"] for call in writes.notified] == [
        NotificationKind.ASSIGNED,
        NotificationKind.STATUS_CHANGED,
    ]


# --- who may watch what -----------------------------------------------


SUBSCRIBE = """
mutation Subscribe($input: IssueSubscriptionInput!) {
  issueSubscribe(input: $input) { subscribed errors { field code } }
}
"""

UNSUBSCRIBE = """
mutation Unsubscribe($input: IssueSubscriptionInput!) {
  issueUnsubscribe(input: $input) { subscribed errors { field code } }
}
"""

SUBSCRIBERS = """
query Watchers($slug: String!, $issueId: UUID!) {
  issueSubscribers(workspaceSlug: $slug, issueId: $issueId) { userId }
}
"""

VIEWER_IS_SUBSCRIBED = """
query Watching($slug: String!, $issueId: UUID!) {
  issueViewerIsSubscribed(workspaceSlug: $slug, issueId: $issueId)
}
"""

SUBSCRIPTION_INPUT = {"workspaceSlug": "acme", "issueId": str(ISSUE_ID)}

DOCUMENTS = [
    (SUBSCRIBE, {"input": SUBSCRIPTION_INPUT}),
    (UNSUBSCRIBE, {"input": SUBSCRIPTION_INPUT}),
    (SUBSCRIBERS, {"slug": "acme", "issueId": str(ISSUE_ID)}),
    (VIEWER_IS_SUBSCRIBED, {"slug": "acme", "issueId": str(ISSUE_ID)}),
]
DOCUMENT_IDS = ["subscribe", "unsubscribe", "subscribers", "viewerIsSubscribed"]


class RefusingMembershipService:
    """Answers every slug the way a non-member's lookup does."""

    def __init__(self):
        self.calls: list[dict] = []

    async def authorized_scope_for_slug(self, *, slug, user_id):
        self.calls.append({"slug": slug, "user_id": user_id})

        raise WorkspaceAccessDeniedError()


def viewer_context(viewer_id, **services):
    context = graphql_context(**services)

    async def viewer():
        return None if viewer_id is None else SimpleNamespace(id=viewer_id)

    context.viewer = viewer

    return context


@pytest.mark.parametrize(("document", "variables"), DOCUMENTS, ids=DOCUMENT_IDS)
async def test_an_anonymous_caller_is_refused_before_any_lookup(document, variables):
    """Fails closed and fails first, on all four fields.

    The membership service is wired and asserted UNTOUCHED, which is the half
    that matters: a resolver that refused after resolving a workspace would
    give the same response and would have performed a protected lookup for a
    request with no identity.
    """
    memberships = RefusingMembershipService()

    result = await schema.execute(
        document,
        variable_values=variables,
        context_value=viewer_context(None, membership_service=memberships),
    )

    assert result.errors is not None
    assert result.errors[0].formatted["message"] == UNAUTHENTICATED_MESSAGE
    assert memberships.calls == [], "no lookup may happen for an anonymous caller"


@pytest.mark.parametrize(("document", "variables"), DOCUMENTS, ids=DOCUMENT_IDS)
async def test_a_workspace_the_viewer_does_not_belong_to_is_not_found(
    document, variables
):
    """The activity service is an `UnusedService`, so reaching it at all fails
    with a sentence naming it -- which is what proves the refusal happens
    before any subscription is touched."""
    memberships = RefusingMembershipService()

    result = await schema.execute(
        document,
        variable_values=variables,
        context_value=viewer_context(VIEWER_ID, membership_service=memberships),
    )

    assert result.errors is not None
    assert result.errors[0].formatted["message"] == WORKSPACE_NOT_FOUND_MESSAGE
    assert result.errors[0].formatted["extensions"] == {"code": "NOT_FOUND"}
    assert memberships.calls == [{"slug": "acme", "user_id": VIEWER_ID}]


def test_no_subscription_field_accepts_a_user_id():
    """The absence that makes every other assertion here hold.

    A `userId` argument on any of the four would be a way to sign somebody
    else up for an issue's notifications, or to ask whether a named person is
    watching a named issue -- a fact about them rather than about the issue.
    The watcher is `scope.user_id` and there is nowhere else it could come
    from.
    """
    sdl = strawberry.printer.print_schema(schema)

    fields = [
        line
        for line in sdl.splitlines()
        if re.search(
            r"^\s+issue(Subscribe|Unsubscribe|Subscribers|ViewerIsSubscribed)\b", line
        )
    ]

    # The four exist. Without this the rest of the test passes on a schema
    # that lost the feature entirely.
    assert len(fields) == 4, fields
    assert not any("userId" in line for line in fields)

    # And the input the two mutations take carries no user either, which is
    # where one would be added by somebody who read only the argument lists.
    payload = sdl.split("input IssueSubscriptionInput {", 1)[1].split("}", 1)[0]

    assert "userId" not in payload

    # `IssueSubscriber.userId` is a field of the RESULT and stays: it is what
    # a client renders an avatar from.
    assert "  userId: UUID!" in sdl
