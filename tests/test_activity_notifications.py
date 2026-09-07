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

import re
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import strawberry

from app.domain.activity import ActivityKind, IssueSnapshot, changes
from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.notifications import (
    NotificationEntity,
    NotificationKind,
    NotificationPage,
)
from app.graphql.schema import build_schema
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.types.activity import ActivityKindEnum
from app.graphql.types.notification import NotificationKindEnum
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE

from tests.conftest import (
    BASE_TIME,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    TEST_WORKSPACE_SLUG,
    graphql_context,
    make_entity,
)


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


# `CONSTRAINT <name>_kind_known CHECK (kind IN ('a', 'b'))`, however it is laid
# out. Both spellings in the tree are matched: 012 writes the activity one with
# the vocabulary on twelve indented lines, and 020 writes the notification one
# on one line inside an ALTER TABLE.
_KIND_CHECK = re.compile(
    r"CONSTRAINT\s+(\w+_kind_known)\s+CHECK\s*\(\s*kind\s+IN\s*\(([^)]*)\)",
    re.IGNORECASE,
)


def _admitted_kinds() -> dict[str, set[str]]:
    """The vocabulary each `*_kind_known` CHECK admits, as the schema now is.

    Every migration in filename order, keeping the LAST declaration of each
    constraint name, because a CHECK is widened by dropping and re-adding it --
    which is how 020 added 'status_changed' to `notifications_kind_known`, and
    how the next kind will arrive too. Reading only the file that first
    declared a constraint would make this gate fail on every widening, for a
    reason that has nothing to do with the vocabulary being out of step.

    Read out of the migration text rather than restated here, so this test
    cannot drift into agreeing with itself.
    """
    migrations = Path(__file__).resolve().parent.parent / "migrations"
    admitted: dict[str, set[str]] = {}

    for path in sorted(migrations.glob("*.sql")):
        for name, values in _KIND_CHECK.findall(path.read_text(encoding="utf-8")):
            admitted[name.lower()] = set(re.findall(r"'([^']*)'", values))

    return admitted


@pytest.mark.parametrize(
    ("constraint", "enum"),
    [
        ("issue_activity_kind_known", ActivityKind),
        ("notifications_kind_known", NotificationKind),
    ],
)
def test_every_kind_is_exactly_what_the_schema_admits(constraint, enum):
    """The third copy of each vocabulary is the CHECK in the migrations.

    Equality rather than membership, in both directions. A kind this code can
    write and the column refuses is a runtime failure on whichever path first
    produces it; a kind the column admits and no enum names is a value that can
    reach the table by hand and then fail to convert when it is read back --
    `ActivityRepository._to_entity` raises on one.
    """
    admitted = _admitted_kinds()

    assert constraint in admitted, (
        f"no migration declares {constraint}; the regex in this file has "
        "stopped matching the schema it is meant to read"
    )
    assert admitted[constraint] == {kind.value for kind in enum}


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


# --- naming the issue an inbox row is about ---------------------------


INBOX_WITH_ISSUES = """
query Inbox($slug: String!) {
  notifications(workspaceSlug: $slug) {
    nodes {
      id
      issue {
        id
        identifier
        title
      }
    }
  }
}
"""

INBOX_ISSUE_IDS = [
    UUID("00000000-0000-7000-8000-000000000101"),
    UUID("00000000-0000-7000-8000-000000000102"),
]


class FakeInboxService:
    """One page of notifications over a fixed set of issues."""

    def __init__(self, issue_ids):
        self._issue_ids = list(issue_ids)

    async def list_notifications(self, *, scope, unread_only, first, after):
        return NotificationPage(
            nodes=[
                NotificationEntity(
                    id=UUID(int=index + 1),
                    user_id=VIEWER_ID,
                    actor_id=None,
                    issue_id=issue_id,
                    kind=NotificationKind.ASSIGNED,
                    read_at=None,
                    created_at=BASE_TIME,
                )
                for index, issue_id in enumerate(self._issue_ids)
            ],
            has_next_page=False,
            end_cursor=None,
        )


class RecordingIssueService:
    """Answers `get_many_by_ids` and records every batch it was asked for."""

    def __init__(self, known):
        self._known = {entity.id: entity for entity in known}
        self.calls: list[dict] = []

    async def get_many_by_ids(self, *, scope, issue_ids):
        self.calls.append({"scope": scope, "issue_ids": list(issue_ids)})

        return [self._known[i] for i in issue_ids if i in self._known]


def _inbox_entity(issue_id: UUID, index: int):
    return make_entity(index, id=issue_id)


async def test_an_inbox_page_costs_one_issue_query_and_not_one_per_row():
    """The whole reason this field is a loader rather than a lookup.

    Twenty-five rows in an inbox naming twenty-five issues is twenty-five
    round trips without batching, and the complexity rule cannot catch it:
    `issue` declares no page size, so it is charged as a single selection
    whatever it costs to resolve.
    """
    issues = RecordingIssueService(
        [_inbox_entity(issue_id, i + 1) for i, issue_id in enumerate(INBOX_ISSUE_IDS)]
    )

    result = await schema.execute(
        INBOX_WITH_ISSUES,
        variable_values={"slug": TEST_WORKSPACE_SLUG},
        context_value=viewer_context(
            TEST_USER_ID,
            activity_service=FakeInboxService(INBOX_ISSUE_IDS),
            issue_service=issues,
        ),
    )

    assert result.errors is None
    assert len(issues.calls) == 1
    assert sorted(issues.calls[0]["issue_ids"]) == sorted(INBOX_ISSUE_IDS)

    nodes = result.data["notifications"]["nodes"]

    assert [node["issue"]["id"] for node in nodes] == [
        str(issue_id) for issue_id in INBOX_ISSUE_IDS
    ]
    assert all(node["issue"]["identifier"].startswith("ENG-") for node in nodes)


async def test_two_rows_about_the_same_issue_are_one_key():
    """The loader's cache is the reason a busy issue does not cost more."""
    issue_id = INBOX_ISSUE_IDS[0]
    issues = RecordingIssueService([_inbox_entity(issue_id, 1)])

    result = await schema.execute(
        INBOX_WITH_ISSUES,
        variable_values={"slug": TEST_WORKSPACE_SLUG},
        context_value=viewer_context(
            TEST_USER_ID,
            activity_service=FakeInboxService([issue_id, issue_id, issue_id]),
            issue_service=issues,
        ),
    )

    assert result.errors is None
    assert issues.calls[0]["issue_ids"] == [issue_id]


async def test_the_batch_is_scoped_to_the_workspace_the_field_authorized():
    """The tenant comes from the authorized scope, never from the row.

    A notification carries an `issue_id` and nothing else; if the batch were
    keyed on that alone, a mismatched row -- or a loader shared past the
    request -- could answer with an issue from another workspace. The
    workspace in the key is the one `authorized_scope` returned.
    """
    issues = RecordingIssueService([])

    await schema.execute(
        INBOX_WITH_ISSUES,
        variable_values={"slug": TEST_WORKSPACE_SLUG},
        context_value=viewer_context(
            TEST_USER_ID,
            activity_service=FakeInboxService(INBOX_ISSUE_IDS),
            issue_service=issues,
        ),
    )

    assert issues.calls[0]["scope"].workspace_id == TEST_WORKSPACE_ID


async def test_an_issue_the_batch_cannot_see_resolves_to_null():
    """Archived, absent and another tenant's are one answer, not three."""
    issues = RecordingIssueService([])

    result = await schema.execute(
        INBOX_WITH_ISSUES,
        variable_values={"slug": TEST_WORKSPACE_SLUG},
        context_value=viewer_context(
            TEST_USER_ID,
            activity_service=FakeInboxService(INBOX_ISSUE_IDS),
            issue_service=issues,
        ),
    )

    assert result.errors is None
    assert [node["issue"] for node in result.data["notifications"]["nodes"]] == [
        None,
        None,
    ]
