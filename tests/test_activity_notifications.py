"""Activity and notifications without a database.

Three things are checked here and nothing else -- the rest needs a server and
lives in tests/test_activity_notifications_db.py:

  * the two enums the domain declares and the two the schema publishes say the
    same thing, which is a claim their own docstrings make and nothing else
    would notice breaking;
  * `changes` records what moved and stays silent about what did not;
  * the inbox resolvers refuse an anonymous caller before touching a service,
    and refuse a workspace the caller does not belong to with the same
    NOT_FOUND a nonexistent one gets.

The third is the security property of the whole feature, and it is testable
without a database precisely because it must be settled before any data is
read.
"""

from types import SimpleNamespace
from uuid import UUID

import pytest
import strawberry

from app.domain.activity import ActivityKind, IssueSnapshot, changes
from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.notifications import NotificationKind
from app.graphql.schema import build_schema
from app.graphql.types.activity import ActivityKindEnum
from app.graphql.types.notification import NotificationKindEnum
from app.graphql.viewer import (
    UNAUTHENTICATED_MESSAGE,
    WORKSPACE_NOT_FOUND_MESSAGE,
)

from tests.conftest import graphql_context


schema = build_schema("test")

VIEWER_ID = UUID("00000000-0000-7000-8000-0000000000c1")
STATE_ONE = UUID("00000000-0000-7000-8000-0000000000d1")
STATE_TWO = UUID("00000000-0000-7000-8000-0000000000d2")
ASSIGNEE = UUID("00000000-0000-7000-8000-0000000000c2")


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


# --- the vocabularies, said twice -------------------------------------


@pytest.mark.parametrize(
    ("domain", "published"),
    [(ActivityKind, ActivityKindEnum), (NotificationKind, NotificationKindEnum)],
)
def test_the_published_enum_says_what_the_domain_enum_says(domain, published):
    """Two copies exist on purpose; this is what keeps them one vocabulary.

    The transport enum cannot be the domain enum -- decorating a domain class
    with `strawberry.enum` puts a transport attribute on the layer that is not
    allowed to carry one -- so the values are duplicated. A member added to one
    and forgotten in the other is a kind the API can never return, or one it
    can return and nothing can construct, and neither shows up in either
    file's diff.
    """
    assert {member.name: member.value for member in domain} == {
        member.name: member.value for member in published
    }


def test_every_activity_kind_is_one_the_migration_admits():
    """The third copy of the vocabulary is `issue_activity_kind_known`.

    Read out of the migration text rather than restated here, so this test
    cannot drift into agreeing with itself.
    """
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "012_activity_notifications.sql"
    ).read_text(encoding="utf-8")

    for kind in ActivityKind:
        assert f"'{kind.value}'" in sql, f"{kind.value} is not in migration 012"

    for kind in NotificationKind:
        assert f"'{kind.value}'" in sql, f"{kind.value} is not in migration 012"


# --- what an update is worth recording --------------------------------


def test_a_field_that_did_not_move_produces_no_row():
    """The rule that keeps a timeline readable.

    Every update rewrites every column -- the repository's CASE expressions
    assign each one whether or not the patch mentioned it -- so "the statement
    wrote it" cannot be the test for "it changed". A timeline full of "changed
    the title from X to X" is one nobody reads.
    """
    assert changes(snapshot(), snapshot()) == []


def test_each_moved_field_becomes_one_row_naming_both_sides():
    before = snapshot()
    after = snapshot(title="Renamed", priority=3)

    assert changes(before, after) == [
        (ActivityKind.TITLE_CHANGED, "Original", "Renamed"),
        (ActivityKind.PRIORITY_CHANGED, "1", "3"),
    ]


def test_clearing_a_field_is_a_change_and_re_clearing_it_is_not():
    """None is a value here, not an absence.

    Unassigning an issue is an event somebody wants in the history, and
    unassigning an already-unassigned one is not.
    """
    assert changes(snapshot(assignee_id=ASSIGNEE), snapshot()) == [
        (ActivityKind.ASSIGNEE_CHANGED, str(ASSIGNEE), None)
    ]
    assert changes(snapshot(), snapshot()) == []


def test_the_order_of_the_rows_does_not_depend_on_which_fields_moved():
    """Two changes in one request always land title-first.

    They share a `created_at` to the microsecond, so nothing downstream can
    re-derive the order; a stable one here is what makes the timeline the same
    on every read.
    """
    moved = changes(
        snapshot(),
        snapshot(title="Renamed", workflow_state_id=STATE_TWO, priority=4),
    )

    assert [kind for kind, _, _ in moved] == [
        ActivityKind.TITLE_CHANGED,
        ActivityKind.STATE_CHANGED,
        ActivityKind.PRIORITY_CHANGED,
    ]


# --- who may read an inbox --------------------------------------------


NOTIFICATIONS = """
query Inbox($slug: String!) {
  notifications(workspaceSlug: $slug) {
    nodes { id kind }
  }
}
"""

UNREAD_COUNT = """
query Badge($slug: String!) {
  notificationUnreadCount(workspaceSlug: $slug)
}
"""

MARK_READ = """
mutation MarkRead($input: NotificationMarkReadInput!) {
  notificationMarkRead(input: $input) {
    notification { id }
    errors { field code }
  }
}
"""

MARK_ALL_READ = """
mutation MarkAll($input: NotificationMarkAllReadInput!) {
  notificationMarkAllRead(input: $input) {
    markedCount
  }
}
"""

NOTIFICATION_ID = UUID("00000000-0000-7000-8000-0000000000e1")

DOCUMENTS = [
    (NOTIFICATIONS, {"slug": "acme"}),
    (UNREAD_COUNT, {"slug": "acme"}),
    (MARK_READ, {"input": {"workspaceSlug": "acme", "id": str(NOTIFICATION_ID)}}),
    (MARK_ALL_READ, {"input": {"workspaceSlug": "acme"}}),
]
DOCUMENT_IDS = ["notifications", "unreadCount", "markRead", "markAllRead"]


class RefusingMembershipService:
    """Answers every slug the way a non-member's lookup does.

    One refusal for "no such workspace" and for "not yours", because
    `MembershipRepository` returns the same absent row for both -- see
    `WorkspaceAccessDeniedError`.
    """

    def __init__(self):
        self.calls: list[dict] = []

    async def authorized_scope_for_slug(self, *, slug, user_id):
        self.calls.append({"slug": slug, "user_id": user_id})

        raise WorkspaceAccessDeniedError()


def viewer_context(viewer_id, **services):
    """A context whose `viewer()` answers, as the real one's does."""
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
        context_value=viewer_context(
            None,
            membership_service=memberships,
        ),
    )

    assert result.errors is not None
    assert result.errors[0].formatted["message"] == UNAUTHENTICATED_MESSAGE
    assert result.errors[0].formatted["extensions"] == {"code": "UNAUTHENTICATED"}
    assert memberships.calls == [], "no lookup may happen for an anonymous caller"


@pytest.mark.parametrize(("document", "variables"), DOCUMENTS, ids=DOCUMENT_IDS)
async def test_a_workspace_the_viewer_does_not_belong_to_is_not_found(
    document, variables
):
    """A slug is a public string, so it selects what is asked about and never
    who is asking.

    The activity service is an `UnusedService`, so reaching it at all fails
    with a sentence naming it -- which is what proves the refusal happens
    before any inbox is touched, rather than after a query that returned
    nothing.
    """
    memberships = RefusingMembershipService()

    result = await schema.execute(
        document,
        variable_values=variables,
        context_value=viewer_context(
            VIEWER_ID,
            membership_service=memberships,
        ),
    )

    assert result.errors is not None
    assert result.errors[0].formatted["message"] == WORKSPACE_NOT_FOUND_MESSAGE
    assert result.errors[0].formatted["extensions"] == {"code": "NOT_FOUND"}

    # The lookup was made against the AUTHENTICATED viewer, not against
    # anything the document carried.
    assert memberships.calls == [{"slug": "acme", "user_id": VIEWER_ID}]


def test_no_inbox_field_accepts_a_user_id():
    """The absence that makes every other assertion here hold.

    A `userId` argument anywhere on these four would be a way to read or
    clear somebody else's inbox by sending their id, and no amount of
    checking at the call site would keep one call site from forgetting. The
    recipient is `scope.user_id` and there is nowhere else it could come
    from.
    """
    sdl = strawberry.printer.print_schema(schema)

    inbox = [
        line
        for line in sdl.splitlines()
        if "notification" in line.lower() and "userId" in line
    ]

    assert inbox == []
