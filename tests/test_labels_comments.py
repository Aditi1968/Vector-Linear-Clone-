"""Labels and comments below the database: validation, SQL, and the schema.

tests/test_labels_comments_db.py asks a real server whether a cross-tenant row
can exist. This file asks the questions a server cannot answer cheaply, and
one it cannot answer at all:

  * Section A -- the rules the service enforces before a connection is ever
    taken, over a pool that fails if one is.
  * Section B -- the statements the repositories send, read against a fake
    connection. A server proves a query returns the right rows; only the text
    proves it is a keyset comparison rather than an OFFSET, and that the
    tenant arrives as a bound parameter rather than as interpolated SQL.
  * Section C -- the GraphQL surface. That a comment's author cannot be named
    by the client, that an unauthenticated caller is refused before any
    service is reached, and that the nested `Issue.comments` field is priced
    by the complexity rule the way its own docstring claims.

Nothing here starts a container or reads DATABASE_URL.
"""

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from graphql import parse, specified_rules, validate

from app.domain.comments import CommentEntity
from app.domain.errors import ValidationError
from app.domain.labels import LabelEntity
from app.domain.pagination import (
    InvalidCursorError,
    IssuePage,
    decode_label_cursor,
    encode_label_cursor,
)
from app.domain.tenancy import AuthorizedWorkspaceScope, WorkspaceScope
from app.graphql.limits import MAX_COMPLEXITY, OperationLimitsRule
from app.graphql.schema import build_schema
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE
from app.repositories.comments import CommentRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.labels import LabelRepository
from app.services.comments import CommentService
from app.services.labels import (
    DEFAULT_COLOR,
    LABELS_PER_ISSUE_MAX,
    NAME_MAX_LENGTH,
    LabelService,
)

from tests.conftest import (
    TEST_SCOPE,
    TEST_WORKSPACE_ID,
    TEST_WORKSPACE_SLUG,
    ExplodingPool,
    FakeConnection,
    FakeMembershipService,
    FakePool,
    graphql_context,
    make_entity,
    normalize,
)


LABEL_ID = UUID("00000000-0000-7000-8000-0000000000a1")
ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000a2")
COMMENT_ID = UUID("00000000-0000-7000-8000-0000000000a3")
VIEWER_ID = UUID("00000000-0000-7000-8000-0000000000a4")

CREATED_AT = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


def label_service(pool) -> LabelService:
    return LabelService(
        pool=pool,
        repository=LabelRepository(),
        issue_label_repository=IssueLabelRepository(),
    )


def label_row(name="bug", color=DEFAULT_COLOR) -> dict:
    """asyncpg.Record supports __getitem__, which a dict models well enough."""
    return {
        "id": LABEL_ID,
        "name": name,
        "color": color,
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }


def comment_row(body="Reproduced.", author_id=VIEWER_ID) -> dict:
    return {
        "id": COMMENT_ID,
        "issue_id": ISSUE_ID,
        "author_id": author_id,
        "body": body,
        "edited_at": None,
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }


# --------------------------------------------------------------------------
# A. Rules enforced before a connection is taken
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "color", "expected"),
    [
        ("", DEFAULT_COLOR, [("name", "REQUIRED")]),
        ("x" * (NAME_MAX_LENGTH + 1), DEFAULT_COLOR, [("name", "TOO_LONG")]),
        ("bug", "6b7280", [("color", "INVALID_FORMAT")]),
        ("bug", "#6b728", [("color", "INVALID_FORMAT")]),
        ("bug", "#gggggg", [("color", "INVALID_FORMAT")]),
        ("", "nope", [("name", "REQUIRED"), ("color", "INVALID_FORMAT")]),
    ],
    ids=["empty", "too-long", "no-hash", "five-digits", "not-hex", "both"],
)
async def test_invalid_label_attributes_never_reach_the_pool(name, color, expected):
    """Every violation is collected, then raised once, in a fixed order.

    The pool explodes if anything acquires a connection, which is the half of
    this that a message assertion cannot make: validation that ran after the
    INSERT would produce the same structured error while having already opened
    a transaction to do it.
    """
    pool = ExplodingPool()

    with pytest.raises(ValidationError) as raised:
        await label_service(pool).create(scope=TEST_SCOPE, name=name, color=color)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == expected
    assert pool.acquire_count == 0


async def test_a_name_at_the_ceiling_is_accepted():
    """The boundary from the other side: the max length is inclusive.

    Reaching the pool IS the assertion. On valid input the acquire is the
    first thing that happens, so an ExplodingPool's own error is proof that
    validation let the name through.
    """
    pool = ExplodingPool()

    with pytest.raises(AssertionError, match="must not be called"):
        await label_service(pool).create(
            scope=TEST_SCOPE,
            name="x" * NAME_MAX_LENGTH,
            color=DEFAULT_COLOR,
        )


async def test_a_colour_is_folded_to_lowercase_and_a_name_is_not():
    """One is a machine value with a canonical spelling; the other is prose.

    '#FFFFFF' is what a colour picker sends and `labels_color_format` admits
    lowercase only, so folding here is what keeps a legitimate input from
    arriving as a constraint violation with nothing useful in it. A NAME is
    displayed back to the person who wrote it, so its capitalisation -- and
    its whitespace -- are theirs and are stored verbatim.
    """
    connection = FakeConnection(row=label_row(name="  Bug  ", color="#ffffff"))

    await label_service(FakePool(connection)).create(
        scope=TEST_SCOPE,
        name="  Bug  ",
        color="#FFFFFF",
    )

    assert connection.queries[0]["args"] == (TEST_WORKSPACE_ID, "  Bug  ", "#ffffff")


async def test_an_unchosen_colour_becomes_the_services_default():
    """None means "no preference", not "no colour".

    The default lives here rather than on the column, because a database
    default outlives the migration and silently colours every insert that
    forgot one -- and rather than in the GraphQL input, because a default
    declared in the transport is a rule only GraphQL callers get.
    """
    connection = FakeConnection(row=label_row())

    await label_service(FakePool(connection)).create(
        scope=TEST_SCOPE, name="bug", color=None
    )

    assert connection.queries[0]["args"][2] == DEFAULT_COLOR


@pytest.mark.parametrize("first", [0, -1, 101])
async def test_an_out_of_range_page_size_never_reaches_the_pool(first):
    """`first` is refused, never silently clamped.

    A clamp answers a request nobody made: a client asking for 500 and being
    handed 100 has no way to know its page is short, so it stops walking.
    """
    pool = ExplodingPool()

    with pytest.raises(ValidationError) as raised:
        await label_service(pool).list(scope=TEST_SCOPE, first=first, after=None)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("first", "OUT_OF_RANGE")
    ]
    assert pool.acquire_count == 0


@pytest.mark.parametrize(
    "body",
    ["", "x" * 16385],
    ids=["empty", "too-long"],
)
async def test_an_invalid_comment_body_never_reaches_the_pool(body):
    pool = ExplodingPool()
    service = CommentService(pool=pool, repository=CommentRepository())

    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            issue_id=ISSUE_ID,
            author_id=VIEWER_ID,
            body=body,
        )

    assert [issue.field for issue in raised.value.issues] == ["body"]
    assert pool.acquire_count == 0


# --------------------------------------------------------------------------
# B. The statements the repositories send
# --------------------------------------------------------------------------


async def test_the_label_listing_is_a_keyset_walk_and_never_an_offset():
    """The one property a server cannot demonstrate from the rows alone.

    An OFFSET page returns the right rows too, right up until a concurrent
    insert shifts the window and a walk starts skipping. The tenant is ANDed
    with the cursor rather than folded into the row-value comparison, because
    widening it to `(workspace_id, name, id) > (...)` puts workspaces into the
    ordering -- which is how a page walk falls out of one tenant and into
    whichever one sorts next.
    """
    connection = FakeConnection(rows=[])

    await LabelRepository().list(
        connection,
        scope=TEST_SCOPE,
        limit=3,
        after_name="bug",
        after_id=LABEL_ID,
    )

    query = normalize(connection.queries[0]["query"])

    assert "WHERE workspace_id = $1 AND (name, id) > ($2, $3)" in query
    assert "ORDER BY name, id" in query
    assert "OFFSET" not in query.upper()

    # Bound as parameters, never interpolated into the SQL.
    assert connection.queries[0]["args"] == (TEST_WORKSPACE_ID, "bug", LABEL_ID, 3)
    assert str(TEST_WORKSPACE_ID) not in query


async def test_the_comment_thread_is_a_keyset_walk_scoped_to_both_keys():
    connection = FakeConnection(rows=[])

    await CommentRepository().list_for_issue(
        connection,
        scope=TEST_SCOPE,
        issue_id=ISSUE_ID,
        limit=3,
        after_created_at=CREATED_AT,
        after_id=COMMENT_ID,
    )

    query = normalize(connection.queries[0]["query"])

    assert (
        "WHERE workspace_id = $1 AND issue_id = $2 AND (created_at, id) > ($3, $4)"
        in query
    )
    assert "ORDER BY created_at, id" in query
    assert "OFFSET" not in query.upper()


async def test_deleting_a_comment_matches_the_author_in_the_where_clause():
    """Authorship is a predicate, not a comparison made after a read.

    A read-then-delete would have to fetch another author's row into this
    process to discover it was not the viewer's, and would leave a window in
    which the row could change between the two statements.
    """
    connection = FakeConnection(value=None)

    deleted = await CommentRepository().delete(
        connection,
        scope=TEST_SCOPE,
        comment_id=COMMENT_ID,
        author_id=VIEWER_ID,
    )

    query = normalize(connection.queries[0]["query"])

    assert "WHERE workspace_id = $1 AND id = $2 AND author_id = $3" in query
    assert connection.queries[0]["args"] == (TEST_WORKSPACE_ID, COMMENT_ID, VIEWER_ID)

    # No row matched: another author's comment, another tenant's comment, and
    # an id that exists nowhere are one answer.
    assert deleted is False


async def test_the_batched_label_read_binds_the_id_list_as_one_parameter():
    """`= ANY($2)`, not an IN list assembled by string formatting.

    The statement text is therefore fixed whatever the batch size, which is
    what keeps a DataLoader from minting a new prepared statement per page.
    """
    connection = FakeConnection(rows=[])

    await IssueLabelRepository().list_for_issues(
        connection,
        scope=TEST_SCOPE,
        issue_ids=[ISSUE_ID],
    )

    query = normalize(connection.queries[0]["query"])

    assert "issue_labels.issue_id = ANY($2::uuid[])" in query
    assert str(ISSUE_ID) not in query

    # The join pins the label to the same workspace as the association. That
    # is already guaranteed by issue_labels_label_fk, and is written anyway: a
    # read that silently depends on a constraint starts crossing tenants the
    # day the constraint is relaxed.
    assert "ON labels.workspace_id = issue_labels.workspace_id" in query


def test_a_label_cursor_round_trips_its_stored_name():
    """The name goes in verbatim and comes back verbatim.

    Folding it would mint a cursor the server cannot compare against the
    column it ordered by: Python's `str.lower()` and PostgreSQL's `lower()`
    disagree on some Unicode strings, and the disagreement surfaces as a page
    walk that skips or repeats a row.
    """
    decoded = decode_label_cursor(encode_label_cursor("İstanbul", LABEL_ID))

    assert decoded.name == "İstanbul"
    assert decoded.id == LABEL_ID


@pytest.mark.parametrize(
    "cursor",
    ["", "not-base64!!", "e30=", "eyJuYW1lIjogMX0=", "WyJidWciLCAxXQ=="],
    ids=["empty", "not-base64", "empty-object", "name-not-a-string", "a-list"],
)
def test_every_malformed_label_cursor_is_one_error(cursor):
    """Cursors are opaque, so no parser detail may reach a client."""
    with pytest.raises(InvalidCursorError):
        decode_label_cursor(cursor)


# --------------------------------------------------------------------------
# C. The GraphQL surface
# --------------------------------------------------------------------------


schema = build_schema("test")

COMMENT_CREATE = """
mutation Comment($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    comment { id body authorId }
    errors { field code message }
  }
}
"""

COMMENT_DELETE = """
mutation Delete($input: CommentDeleteInput!) {
  commentDelete(input: $input) {
    deletedCommentId
    errors { field code message }
  }
}
"""


class ExplodingCommentService:
    """Fails if anything calls it. An unauthenticated caller must not."""

    async def create(self, **kwargs):
        raise AssertionError("an unauthenticated caller reached the service")

    async def delete(self, **kwargs):
        raise AssertionError("an unauthenticated caller reached the service")


class FakeCommentService:
    """Records every call, so "what was the author?" is answerable."""

    def __init__(self, entity=None):
        self._entity = entity
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)

        return self._entity

    async def delete(self, **kwargs):
        self.calls.append(kwargs)

        return kwargs["comment_id"]


def viewer_context(viewer_id, **services):
    """A context whose `viewer()` answers, as the real one's does.

    Awaited rather than read, mirroring `VectorContext.viewer`: the identity
    comes from the request's session cookie, so it is resolved and never a
    value a caller sets. `None` is what the session layer produces for a
    request with no cookie, an expired one, or one naming no session.

    The membership fake is built to agree with it -- the scope it hands back
    carries the same user id -- because a comment's author now comes off that
    scope rather than off a second lookup. Two ids that could disagree here
    would make the assertions below pass against a resolver reading the wrong
    one.
    """
    services.setdefault(
        "membership_service",
        FakeMembershipService(
            scope=AuthorizedWorkspaceScope(
                workspace_id=TEST_WORKSPACE_ID,
                user_id=viewer_id,
                role="member",
            )
        ),
    )

    context = graphql_context(**services)

    async def viewer():
        return None if viewer_id is None else SimpleNamespace(id=viewer_id)

    context.viewer = viewer

    return context


def test_the_comment_input_has_no_author_field():
    """The impersonation guard, asserted against the published SDL.

    A resolver that reads the viewer is only half of this. The other half is
    that no client can even ASK to be somebody else -- an `authorId` here
    would be an impersonation API, and one that is hard to remove once
    anything has generated types against it.
    """
    sdl = str(schema)

    # No INPUT type anywhere in the schema names an author -- not just this
    # one, because the field would be as dangerous on `CommentUpdateInput` the
    # day someone adds it.
    for block in sdl.split("input ")[1:]:
        body = block.split("}", 1)[0]

        assert "author" not in body.lower(), f"an input type names an author: {body}"

    # And the OUTPUT type does carry it: a client may read who wrote a comment,
    # which is what makes the absence above a deliberate asymmetry rather than
    # a field nobody got round to.
    assert "authorId: UUID!" in sdl


@pytest.mark.parametrize(
    ("document", "variables"),
    [
        (
            COMMENT_CREATE,
            {
                "input": {
                    "workspaceSlug": TEST_WORKSPACE_SLUG,
                    "issueId": str(ISSUE_ID),
                    "body": "hi",
                }
            },
        ),
        (
            COMMENT_DELETE,
            {"input": {"workspaceSlug": TEST_WORKSPACE_SLUG, "id": str(COMMENT_ID)}},
        ),
    ],
    ids=["commentCreate", "commentDelete"],
)
async def test_an_unauthenticated_comment_mutation_is_refused_before_any_lookup(
    document, variables
):
    """UNAUTHENTICATED, and the service is never asked anything.

    The exploding fake is the half a message assertion cannot make: a resolver
    that wrote first and checked afterwards would produce the same response
    having already written the row.
    """
    result = await schema.execute(
        document,
        variable_values=variables,
        context_value=viewer_context(None, comment_service=ExplodingCommentService()),
    )

    assert result.errors is not None
    assert len(result.errors) == 1

    formatted = result.errors[0].formatted

    assert formatted["message"] == UNAUTHENTICATED_MESSAGE
    assert formatted["extensions"] == {"code": "UNAUTHENTICATED"}


async def test_the_author_is_the_viewer_and_not_anything_the_client_sent():
    """The positive half: what actually reaches the service.

    Asserted on the argument rather than on the response, because a resolver
    that echoed the right id back while writing a different one would satisfy
    any assertion made on the payload.
    """
    service = FakeCommentService(entity=CommentEntity(**comment_row()))

    result = await schema.execute(
        COMMENT_CREATE,
        variable_values={
            "input": {
                "workspaceSlug": TEST_WORKSPACE_SLUG,
                "issueId": str(ISSUE_ID),
                "body": "Reproduced.",
            }
        },
        context_value=viewer_context(VIEWER_ID, comment_service=service),
    )

    assert result.errors is None
    assert service.calls[0]["author_id"] == VIEWER_ID
    assert service.calls[0]["issue_id"] == ISSUE_ID


async def test_deleting_a_comment_passes_the_viewer_as_the_author():
    """The service cannot enforce "only the author" without being told who is
    asking, and the resolver is the only layer that knows."""
    service = FakeCommentService()

    result = await schema.execute(
        COMMENT_DELETE,
        variable_values={
            "input": {"workspaceSlug": TEST_WORKSPACE_SLUG, "id": str(COMMENT_ID)}
        },
        context_value=viewer_context(VIEWER_ID, comment_service=service),
    )

    assert result.errors is None
    assert service.calls[0]["author_id"] == VIEWER_ID
    assert service.calls[0]["comment_id"] == COMMENT_ID


# --- what the nested comment field costs ------------------------------


ISSUES_WITH_COMMENTS = """
query {
  issues(workspaceSlug: "acme") {
    nodes {
      comments {
        nodes { id body }
      }
    }
  }
}
"""

ISSUES_WITH_COMMENTS_SMALL_PAGE = """
query {
  issues(workspaceSlug: "acme", first: 20) {
    nodes {
      comments {
        nodes { id body }
      }
    }
  }
}
"""

ISSUES_WITH_LABELS = """
query {
  issues(workspaceSlug: "acme") {
    nodes {
      id
      labels { id name }
    }
  }
}
"""

ONE_ISSUES_THREAD = """
query {
  issue(workspaceSlug: "acme", id: "00000000-0000-7000-8000-0000000000a2") {
    comments {
      nodes { id body }
    }
  }
}
"""


def complexity_error(document):
    """The refusal this document earns, or None.

    Validated rather than executed, which is what the claim is actually about:
    `OperationLimitsRule` runs while the document is still an AST, before
    graphql-core calls a single resolver, and that IS the property -- a query
    that fans out into thousands of reads must be refused before any of them
    happen. Executing instead would need every service the document touches
    wired up, and would prove the same thing later and less directly.
    """
    errors = validate(
        schema._schema,
        parse(document),
        [*specified_rules, OperationLimitsRule],
    )

    for error in errors:
        if "complexity" in error.message:
            return error.message

    return None


def test_a_full_page_of_issues_carrying_comment_threads_is_refused():
    """The arithmetic `DEFAULT_COMMENT_FIRST` documents, pinned as a test.

    `Issue.comments` declares `first: 20`, so an unqualified `comments`
    selecting two scalars costs 20 * 2 = 40 per issue; on the default page of
    50 issues that is 2000, over MAX_COMPLEXITY. This is the fan-out the
    complexity rule exists to refuse during validation, before graphql-core
    calls a single resolver -- and it is refused precisely BECAUSE the field
    declares a page size. An unpaginated `comments` would be charged once and
    served as an unbounded fan-out.
    """
    message = complexity_error(ISSUES_WITH_COMMENTS)

    assert message is not None
    assert str(MAX_COMPLEXITY) in message


@pytest.mark.parametrize(
    "document",
    [ISSUES_WITH_COMMENTS_SMALL_PAGE, ONE_ISSUES_THREAD],
    ids=["smaller-issue-page", "one-issue"],
)
def test_the_shapes_a_client_should_actually_send_are_within_budget(document):
    """The other half: the budget refuses the fan-out and not the product.

    Without this, "is the limit doing anything" and "is the limit doing too
    much" are the same green test. A thread read on ONE issue is charged once
    and is cheap at any page size the service allows.
    """
    assert complexity_error(document) is None


def test_the_labels_field_is_unpaginated_and_bounded_at_the_write():
    """`Issue.labels` declares no page size, so the rule charges it once.

    That under-prices its true cardinality on a page of issues, which is why
    two other things have to be true, and both are asserted here rather than
    left as prose: the list is bounded at ATTACH time by LABELS_PER_ISSUE_MAX
    -- truncating the READ would silently hide labels an issue really wears --
    and the queries are batched by a DataLoader rather than run per issue.
    """
    sdl = str(schema)

    assert "labels: [Label!]!" in sdl

    assert LABELS_PER_ISSUE_MAX > 0
    assert complexity_error(ISSUES_WITH_LABELS) is None


class FakeIssuePageService:
    """Serves one canned page of issues, so `Issue.labels` has issues to run on."""

    def __init__(self, entities):
        self._entities = entities

    async def list(self, *, scope, team_id, first, after):
        return IssuePage(nodes=self._entities, has_next_page=False, end_cursor=None)


class BatchCountingLabelService:
    """Answers the batched read and counts how many times it was asked.

    The count is the assertion. A correct-looking `Issue.labels` that ran one
    query per issue would return exactly the same response as this one -- the
    N+1 is invisible in the data and visible only here.
    """

    def __init__(self, by_issue):
        self._by_issue = by_issue
        self.batches: list[list[UUID]] = []

    async def labels_for_issues(self, *, scope, issue_ids):
        self.batches.append(list(issue_ids))

        return {
            issue_id: self._by_issue[issue_id]
            for issue_id in issue_ids
            if issue_id in self._by_issue
        }


async def test_a_page_of_issues_costs_one_label_query_and_not_one_each():
    """The DataLoader, end to end through the schema.

    Three issues, one of them wearing nothing. One statement is issued for the
    whole page; each issue gets its own labels back in its own position; and
    the issue with none gets an empty list rather than another issue's answer
    or a null.

    That last case matters beyond tidiness: a key the batch found nothing for
    covers both an issue with no labels and an issue that is not in this
    workspace, and the two must stay indistinguishable -- an id that answered
    differently because it belonged to someone else would report that it is
    real.
    """
    entities = [make_entity(index) for index in range(3)]
    labelled = {
        entities[0].id: [
            LabelEntity(
                id=LABEL_ID,
                name="bug",
                color=DEFAULT_COLOR,
                created_at=CREATED_AT,
                updated_at=CREATED_AT,
            )
        ],
        entities[1].id: [],
    }
    labels = BatchCountingLabelService(labelled)

    result = await schema.execute(
        ISSUES_WITH_LABELS,
        context_value=graphql_context(
            issue_service=FakeIssuePageService(entities),
            label_service=labels,
        ),
    )

    assert result.errors is None

    nodes = result.data["issues"]["nodes"]

    assert [[label["name"] for label in node["labels"]] for node in nodes] == [
        ["bug"],
        [],
        [],
    ]

    # One batch for the whole page, carrying every issue on it.
    assert len(labels.batches) == 1
    assert set(labels.batches[0]) == {entity.id for entity in entities}


async def test_the_label_loader_keys_on_the_workspace_as_well_as_the_issue():
    """A cache keyed by issue id alone is a cross-tenant read waiting to happen.

    The workspace is IN the key, so the loader could not answer one tenant from
    another's cached batch even if a document ever named two workspaces. Read
    off the scope the service was handed, because that is the value the key was
    rebuilt from.
    """
    entities = [make_entity(0)]
    labels = BatchCountingLabelService({})

    captured: list[WorkspaceScope] = []

    async def labels_for_issues(*, scope, issue_ids):
        captured.append(scope)

        return {}

    labels.labels_for_issues = labels_for_issues

    await schema.execute(
        ISSUES_WITH_LABELS,
        context_value=graphql_context(
            issue_service=FakeIssuePageService(entities),
            label_service=labels,
        ),
    )

    # A plain WorkspaceScope carrying the authorized workspace's id, because
    # the loader REBUILDS one from its key rather than closing over the scope
    # the resolver held. That is the property under test: the tenant travels
    # in the key, so a cached batch cannot be served across workspaces.
    assert captured == [TEST_SCOPE]


async def test_attaching_past_the_ceiling_is_refused_rather_than_truncating():
    """The cap that makes an unpaginated `labels` safe to serve.

    Soft by construction: the count and the insert share a transaction but are
    not serialised against each other, so two racing attaches can both observe
    the count one below the cap. The ceiling is therefore this number plus
    however many attaches are in flight -- which is a bound, and bounding the
    list is the whole job. Making it exact would serialise every attach on the
    issue row.
    """
    connection = FakeConnection(value=LABELS_PER_ISSUE_MAX)
    service = label_service(FakePool(connection))

    with pytest.raises(ValidationError) as raised:
        await service.attach(scope=TEST_SCOPE, issue_id=ISSUE_ID, label_id=LABEL_ID)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("labelId", "LIMIT_EXCEEDED")
    ]

    # The count ran; the INSERT did not.
    assert len(connection.queries) == 1
    assert "count(*)" in normalize(connection.queries[0]["query"])


def test_the_domain_entities_carry_no_workspace_id():
    """A tenant on an entity is a second copy of a fact the caller holds.

    Two copies are two things that can disagree, and the one that would be
    believed is the one on the object rather than the one in the scope that
    produced it. `IssueEntity` omits it for the same reason.
    """
    assert not hasattr(
        LabelEntity(
            id=LABEL_ID,
            name="bug",
            color=DEFAULT_COLOR,
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        ),
        "workspace_id",
    )
    assert not hasattr(CommentEntity(**comment_row()), "workspace_id")


def test_a_scope_is_required_at_every_repository_entry_point():
    """No default, no instance attribute, no way in without naming a tenant.

    Read off the signatures rather than asserted per method, so a method added
    later is covered by a test nobody has to remember to extend.
    """
    for repository in (LabelRepository, CommentRepository, IssueLabelRepository):
        for name, method in inspect.getmembers(repository, inspect.isfunction):
            if name.startswith("_"):
                continue

            parameters = inspect.signature(method).parameters

            assert "scope" in parameters, f"{repository.__name__}.{name} has no scope"
            assert parameters["scope"].kind is inspect.Parameter.KEYWORD_ONLY
            assert parameters["scope"].default is inspect.Parameter.empty


def test_a_scope_is_not_a_permission():
    """A WorkspaceScope says which tenant, never that anyone may be there.

    Stated as a test because the type is the thing most likely to be mistaken
    for an authorization: nothing in the label or comment services checks that
    the caller belongs to the workspace they named, and the only reason a
    comment cannot be written into a workspace by a stranger is the composite
    author key in the database.
    """
    scope = WorkspaceScope(workspace_id=TEST_WORKSPACE_ID)

    assert not hasattr(scope, "user_id")
    assert not hasattr(scope, "role")
