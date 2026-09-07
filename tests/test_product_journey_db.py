"""The product as a person uses it: one account, one browser, one journey.

Fifteen feature branches each arrived with their own tests, and every one of
them is honest about the feature it covers. What none of them covers is the
seam between two features, and a seam is where an integration breaks: a
mutation that writes a column the next query does not read, a cursor that
survives its own suite and not the schema above it, a session that works in
the resolver test because the resolver test handed the resolver a viewer.

That last one is the gap this file exists to close. `tests/
test_graphql_workspace_scope_db.py` is the best cross-feature suite here and
it still calls `schema.execute(...)` with a hand-built context and a stubbed
viewer -- which skips the ASGI application, the router, `get_context()`, the
body-limit middleware, the pool the lifespan builds, and the session cookie.
Identity in that suite is an attribute a test assigned. Identity here is a
`Set-Cookie` header the server wrote and an `httpx.AsyncClient` sent back,
which is the only arrangement in which "log out, then try again" means
anything at all.

So everything below drives the real `create_app()` over httpx, against a real
PostgreSQL 18 holding every migration. Two accounts are two clients, because
two cookie jars is what makes them two people. Nothing is stubbed: not the
pool, not the auth service, not the membership lookup.

Four subjects, in the order they matter:

  * `test_a_whole_product_journey_over_http` -- register through to logging
    back in, twenty-odd steps, each one depending on the row the last one
    wrote. A step that fails names the feature that broke the chain.
  * `test_a_second_workspace_reaches_none_of_the_first` -- the isolation
    property over HTTP, where the workspace comes from a membership row keyed
    on the cookie rather than from a stub, and where "not yours" and "not
    there" have to be the same bytes.
  * the session tests -- no cookie, a dead cookie, and a cookie that lives
    across requests.
  * `test_a_filtered_walk_in_a_non_default_order_returns_every_row_once` --
    keyset pagination through the schema rather than through the service,
    plus the cursor's refusal to be replayed under another sort.

Marked `db`: deselected by default, skipped when Docker is unreachable.
Nothing here touches DATABASE_URL or Neon -- the container's DSN is injected
through `use_environment`, which also moves the working directory so the
repository's own `.env` cannot be read.
"""

from pathlib import Path

import asyncpg
import httpx
import pytest

from app.config import get_settings
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE
from app.http_cookies import SESSION_COOKIE_NAME
from app.main import create_app
from scripts.apply_migration import compute_checksum, parse_version

from tests.conftest import apply_all_migrations, reset_schema
from tests.test_settings import use_environment


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# Long enough to be a real password and fixed so that a failing log-in is a
# defect rather than a typo. Registration hashes it with argon2 at RFC 9106's
# low-memory profile, which is ~100ms a call -- the reason this file has four
# accounts in total and not forty.
PASSWORD = "correct horse battery staple"


@pytest.fixture
async def application(postgres_dsn, monkeypatch, tmp_path):
    """The real application, its real pool, and a fully migrated database.

    The schema is rebuilt per test because the container is shared for the
    whole session and a journey that found another test's rows would be
    asserting against a fixture it cannot see.

    The lifespan is entered explicitly. `httpx.ASGITransport` does not run it
    -- which is why every other httpx suite here overrides `get_context` and
    never reaches PostgreSQL -- and running it is the entire point of this
    file: `connect()` builds the pool the resolvers borrow, and the argon2
    warm-up runs, so the first unknown-address log-in costs what a real one
    does. Anything the lifespan gets wrong fails here rather than in
    production.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)
    finally:
        await connection.close()

    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=postgres_dsn,
        ENVIRONMENT="test",
    )

    app = create_app()

    try:
        async with app.router.lifespan_context(app):
            yield app
    finally:
        # monkeypatch restores the environment, but `get_settings` is
        # lru_cached and would otherwise go on answering later tests in this
        # process with the container's DSN.
        get_settings.cache_clear()


def browser(application) -> httpx.AsyncClient:
    """One client is one person: its own cookie jar and nothing else shared.

    There is no header, no token argument and no viewer override anywhere in
    this file. If a request is authenticated it is because this jar held a
    cookie the server set, which is the same reason a browser's request is.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://vector.test",
    )


async def post(client, document, **variables):
    """One GraphQL request over HTTP, returning the parsed body.

    The status code is asserted here rather than at every call site: GraphQL
    answers 200 for an application-level error, so a non-200 is a transport
    failure and the body is the only useful thing to report about it.
    """
    response = await client.post(
        "/graphql",
        json={"query": document, "variables": variables},
    )

    assert response.status_code == 200, response.text

    return response.json()


async def query(client, document, **variables):
    """The `data` of a request that must not have failed at all."""
    body = await post(client, document, **variables)

    assert body.get("errors") is None, body["errors"]

    return body["data"]


async def mutate(client, document, field, **variables):
    """The payload of a mutation that must have succeeded.

    Both error channels are checked, because a mutation has two and they mean
    different things: a top-level error is a refusal (no such workspace, not
    signed in), and `payload.errors` is input the client could correct.
    Asserting only one of them is how a journey step "passes" while writing
    nothing.
    """
    data = await query(client, document, **variables)
    payload = data[field]

    assert payload["errors"] == [], (field, payload["errors"])

    return payload


def session_token(client) -> str:
    """The raw token this client is holding, straight out of its jar."""
    token = client.cookies.get(SESSION_COOKIE_NAME)

    assert token, "the server set no session cookie"

    return token


REGISTER = """
mutation Register($email: String!, $password: String!, $name: String) {
  register(input: {email: $email, password: $password, name: $name}) {
    user { id email name }
    errors { field code message }
  }
}
"""

LOGIN = """
mutation Login($email: String!, $password: String!) {
  login(input: {email: $email, password: $password}) {
    user { id email }
    errors { field code message }
  }
}
"""

WORKSPACE_CREATE = """
mutation WorkspaceCreate($name: String!, $slug: String!) {
  workspaceCreate(input: {name: $name, slug: $slug}) {
    workspace { id slug name }
    errors { field code message }
  }
}
"""

TEAM_CREATE = """
mutation TeamCreate($slug: String!, $name: String!, $key: String!) {
  teamCreate(input: {workspaceSlug: $slug, name: $name, key: $key}) {
    team { id key name workflowStates { id name category position } }
    errors { field code message }
  }
}
"""

ISSUE_CREATE = """
mutation IssueCreate($slug: String!, $team: UUID!, $title: String!) {
  issueCreate(input: {workspaceSlug: $slug, teamId: $team, title: $title}) {
    issue { id identifier number title priority workflowStateId }
    errors { field code message }
  }
}
"""


async def sign_up(client, email, name=None):
    """Register, and hand back the account the server created.

    Registration signs the caller in -- there is one Set-Cookie for both --
    so this is also the only place in a journey that a client acquires an
    identity.
    """
    payload = await mutate(
        client,
        REGISTER,
        "register",
        email=email,
        password=PASSWORD,
        name=name,
    )

    assert payload["user"]["email"] == email

    return payload["user"]


async def state_named(client, slug, team_id, name):
    """One of a team's workflow states, by the name 005's seed gives it.

    Read back from the API rather than assumed, because the whole point of
    `teamCreate` seeding a board is that the ids are the server's to choose.
    """
    data = await query(
        client,
        "query Q($slug: String!) { teams(workspaceSlug: $slug) "
        "{ id workflowStates { id name category } } }",
        slug=slug,
    )

    for team in data["teams"]:
        if team["id"] != str(team_id):
            continue

        for state in team["workflowStates"]:
            if state["name"] == name:
                return state

    raise AssertionError(f"team {team_id} has no workflow state named {name!r}")


# --------------------------------------------------------------------------
# The journey
# --------------------------------------------------------------------------


async def test_a_whole_product_journey_over_http(application):
    """Everything the product does, once, in order, as one person doing it.

    Deliberately one test and not twenty. Each step consumes an id the
    previous step returned, so twenty tests would need nineteen fixtures
    rebuilding the state -- and every one of those fixtures would be a second
    implementation of the journey, written by someone who could get it right
    even where the product does not. The failure this file exists to catch is
    exactly the one a shared fixture hides: step 12 works against rows step 11
    would never have written.

    Read the section headers as the trace. A failure names its step, and the
    step names the seam.
    """
    async with browser(application) as ada:
        # --- register, and be signed in by it --------------------------
        registration = await post(
            ada,
            REGISTER,
            email="ada@example.test",
            password=PASSWORD,
            name="Ada",
        )

        assert registration["data"]["register"]["errors"] == []

        ada_user = registration["data"]["register"]["user"]
        ada_token = session_token(ada)

        # The cookie is the claim. `me` is answered from the session row the
        # registration wrote, found by the token in the jar -- no argument
        # anywhere in the document says who is asking.
        assert await query(ada, "query { me { id email name } }") == {
            "me": {
                "id": ada_user["id"],
                "email": "ada@example.test",
                "name": "Ada",
            }
        }

        # --- a workspace, owned by whoever registered ------------------
        workspace = (
            await mutate(
                ada,
                WORKSPACE_CREATE,
                "workspaceCreate",
                name="Aurora",
                slug="aurora",
            )
        )["workspace"]

        assert workspace["slug"] == "aurora"

        # Owner, and the role came from the membership row the same
        # transaction wrote -- not from anything the client sent.
        assert await query(
            ada,
            "query { myWorkspaces { workspace { slug } role } }",
        ) == {"myWorkspaces": [{"workspace": {"slug": "aurora"}, "role": "OWNER"}]}

        # --- a team, with the board 005 seeds --------------------------
        team = (
            await mutate(
                ada,
                TEAM_CREATE,
                "teamCreate",
                slug="aurora",
                name="Engineering",
                key="ENG",
            )
        )["team"]

        # Five states, in position order, because an issue cannot exist
        # without one to sit in -- `workflow_state_id` is NOT NULL and
        # nothing in the create path invents one.
        assert [state["name"] for state in team["workflowStates"]] == [
            "Backlog",
            "Todo",
            "In Progress",
            "Done",
            "Canceled",
        ]

        # --- an issue ---------------------------------------------------
        issue = (
            await mutate(
                ada,
                ISSUE_CREATE,
                "issueCreate",
                slug="aurora",
                team=team["id"],
                title="Ship the thing",
            )
        )["issue"]

        # The team's key and the number allocated off its counter, which is
        # the one string a person will paste into a chat window.
        assert issue["identifier"] == "ENG-1"
        assert issue["number"] == 1

        # --- edit title, description and priority in one write ----------
        edited = (
            await mutate(
                ada,
                "mutation M($slug: String!, $id: UUID!) { issueUpdate(id: $id, "
                'input: {workspaceSlug: $slug, title: "Ship the thing, properly", '
                'description: "With tests.", priority: 2}) '
                "{ issue { id title description priority } "
                "errors { field code message } } }",
                "issueUpdate",
                slug="aurora",
                id=issue["id"],
            )
        )["issue"]

        assert edited["title"] == "Ship the thing, properly"
        assert edited["description"] == "With tests."
        assert edited["priority"] == 2

        # --- move it across the board -----------------------------------
        in_progress = await state_named(ada, "aurora", team["id"], "In Progress")
        done = await state_named(ada, "aurora", team["id"], "Done")

        assert in_progress["category"] == "STARTED"

        moved = (
            await mutate(
                ada,
                "mutation M($slug: String!, $id: UUID!, $state: UUID!) "
                "{ issueUpdate(id: $id, input: {workspaceSlug: $slug, "
                "workflowStateId: $state}) "
                "{ issue { workflowStateId completedAt } "
                "errors { field code message } } }",
                "issueUpdate",
                slug="aurora",
                id=issue["id"],
                state=in_progress["id"],
            )
        )["issue"]

        assert moved["workflowStateId"] == in_progress["id"]

        # Derived from the state's CATEGORY and never set by the client:
        # a started issue has no completion instant, a completed one does.
        assert moved["completedAt"] is None

        # --- assign it to the only member there is ----------------------
        assigned = (
            await mutate(
                ada,
                "mutation M($slug: String!, $id: UUID!, $who: UUID!) "
                "{ issueUpdate(id: $id, input: {workspaceSlug: $slug, "
                "assigneeId: $who}) "
                "{ issue { assigneeId creatorId } "
                "errors { field code message } } }",
                "issueUpdate",
                slug="aurora",
                id=issue["id"],
                who=ada_user["id"],
            )
        )["issue"]

        assert assigned["assigneeId"] == ada_user["id"]

        # Authorship came from the cookie at create time. There is no input
        # field for it, so this is the session reaching the row.
        assert assigned["creatorId"] == ada_user["id"]

        # --- a label, attached ------------------------------------------
        label = (
            await mutate(
                ada,
                "mutation M($slug: String!) { labelCreate(input: "
                '{workspaceSlug: $slug, name: "urgent", color: "#ff0000"}) '
                "{ label { id name color } errors { field code message } } }",
                "labelCreate",
                slug="aurora",
            )
        )["label"]

        labelled = (
            await mutate(
                ada,
                "mutation M($slug: String!, $id: UUID!, $label: UUID!) "
                "{ issueLabelAttach(input: {workspaceSlug: $slug, issueId: $id, "
                "labelId: $label}) { issue { id labels { id name } } "
                "errors { field code message } } }",
                "issueLabelAttach",
                slug="aurora",
                id=issue["id"],
                label=label["id"],
            )
        )["issue"]

        assert labelled["labels"] == [{"id": label["id"], "name": "urgent"}]

        # --- a comment ---------------------------------------------------
        comment = (
            await mutate(
                ada,
                "mutation M($slug: String!, $id: UUID!) { commentCreate(input: "
                '{workspaceSlug: $slug, issueId: $id, body: "Starting on this."}) '
                "{ comment { id body authorId } "
                "errors { field code message } } }",
                "commentCreate",
                slug="aurora",
                id=issue["id"],
            )
        )["comment"]

        assert comment["authorId"] == ada_user["id"]

        # --- a sub-issue -------------------------------------------------
        child = (
            await mutate(
                ada,
                ISSUE_CREATE,
                "issueCreate",
                slug="aurora",
                team=team["id"],
                title="Write the migration",
            )
        )["issue"]

        assert child["identifier"] == "ENG-2"

        parented = (
            await mutate(
                ada,
                "mutation M($slug: String!, $child: UUID!, $parent: UUID!) "
                "{ issueSetParent(input: {workspaceSlug: $slug, issueId: $child, "
                "parentId: $parent}) { issue { id parent { id identifier } } "
                "errors { field code message } } }",
                "issueSetParent",
                slug="aurora",
                child=child["id"],
                parent=issue["id"],
            )
        )["issue"]

        assert parented["parent"] == {"id": issue["id"], "identifier": "ENG-1"}

        # --- a relation ---------------------------------------------------
        sibling = (
            await mutate(
                ada,
                ISSUE_CREATE,
                "issueCreate",
                slug="aurora",
                team=team["id"],
                title="Delete the old thing",
            )
        )["issue"]

        relation = (
            await mutate(
                ada,
                "mutation M($slug: String!, $source: UUID!, $target: UUID!) "
                "{ issueRelationCreate(input: {workspaceSlug: $slug, "
                "sourceIssueId: $source, targetIssueId: $target, type: RELATED}) "
                "{ relation { id type issue { id identifier } } "
                "errors { field code message } } }",
                "issueRelationCreate",
                slug="aurora",
                source=issue["id"],
                target=sibling["id"],
            )
        )["relation"]

        # The relation reads from the SOURCE's side, so the issue it names is
        # the other end -- which is the only useful thing to render on a page
        # that is already showing the source.
        assert relation["issue"]["id"] == sibling["id"]

        # --- a cycle, holding the issue ------------------------------------
        cycle = (
            await mutate(
                ada,
                "mutation M($slug: String!, $team: UUID!) { cycleCreate(input: "
                "{workspaceSlug: $slug, teamId: $team, number: 1, "
                'name: "Sprint 1", startsAt: "2026-02-02T00:00:00+00:00", '
                'endsAt: "2026-02-16T00:00:00+00:00"}) '
                "{ cycle { id number name teamId } "
                "errors { field code message } } }",
                "cycleCreate",
                slug="aurora",
                team=team["id"],
            )
        )["cycle"]

        in_cycle = (
            await mutate(
                ada,
                "mutation M($slug: String!, $id: UUID!, $cycle: UUID!) "
                "{ issueSetCycle(input: {workspaceSlug: $slug, issueId: $id, "
                "cycleId: $cycle}) { issue { id cycle { id name } } "
                "errors { field code message } } }",
                "issueSetCycle",
                slug="aurora",
                id=issue["id"],
                cycle=cycle["id"],
            )
        )["issue"]

        assert in_cycle["cycle"] == {"id": cycle["id"], "name": "Sprint 1"}

        # --- a project with a milestone, holding the issue -----------------
        project = (
            await mutate(
                ada,
                "mutation M($slug: String!) { projectCreate(input: "
                '{workspaceSlug: $slug, name: "Launch", state: STARTED}) '
                "{ project { id name state } errors { field code message } } }",
                "projectCreate",
                slug="aurora",
            )
        )["project"]

        milestone = (
            await mutate(
                ada,
                "mutation M($slug: String!, $project: UUID!) "
                "{ projectMilestoneCreate(input: {workspaceSlug: $slug, "
                'projectId: $project, name: "Beta", targetDate: "2026-03-01"}) '
                "{ milestone { id name projectId } "
                "errors { field code message } } }",
                "projectMilestoneCreate",
                slug="aurora",
                project=project["id"],
            )
        )["milestone"]

        in_project = (
            await mutate(
                ada,
                "mutation M($slug: String!, $id: UUID!, $project: UUID!, "
                "$milestone: UUID!) { issueSetProject(input: {workspaceSlug: $slug, "
                "issueId: $id, projectId: $project, milestoneId: $milestone}) "
                "{ issue { id projectId milestoneId "
                "project { id name milestones { id name } } } "
                "errors { field code message } } }",
                "issueSetProject",
                slug="aurora",
                id=issue["id"],
                project=project["id"],
                milestone=milestone["id"],
            )
        )["issue"]

        assert in_project["projectId"] == project["id"]
        assert in_project["milestoneId"] == milestone["id"]
        assert in_project["project"]["milestones"] == [
            {"id": milestone["id"], "name": "Beta"}
        ]

        # --- find it again through every filter it now satisfies ------------
        #
        # Three filters over three features -- assignment, projects, cycles --
        # each one written by a different branch, all narrowing the same list.
        # A filter that read the wrong column would return the empty page,
        # which is why the sibling issues exist: they share the workspace and
        # the team and satisfy none of these.
        list_document = (
            "query Q($slug: String!, $filter: IssueFilterInput!) "
            "{ issues(workspaceSlug: $slug, filter: $filter) "
            "{ totalCount nodes { id identifier } } }"
        )

        for name, issue_filter in (
            ("assignee", {"assigneeId": ada_user["id"]}),
            ("project", {"projectId": project["id"]}),
            ("cycle", {"cycleId": cycle["id"]}),
            (
                "all three",
                {
                    "assigneeId": ada_user["id"],
                    "projectId": project["id"],
                    "cycleId": cycle["id"],
                },
            ),
        ):
            listed = await query(
                ada, list_document, slug="aurora", filter=issue_filter
            )
            found = listed["issues"]

            assert found["totalCount"] == 1, name
            assert [node["identifier"] for node in found["nodes"]] == ["ENG-1"], name

        # The control: the workspace really does hold more than the one issue,
        # so the pages above were narrowed rather than merely small.
        everything = await query(
            ada,
            "query Q($slug: String!) { issues(workspaceSlug: $slug) "
            "{ totalCount nodes { identifier } } }",
            slug="aurora",
        )

        assert everything["issues"]["totalCount"] == 3

        # --- search, by title and by identifier -----------------------------
        by_title = await query(
            ada,
            "query Q($slug: String!, $q: String!) "
            "{ search(workspaceSlug: $slug, query: $q) "
            "{ issues { id identifier title } projects { id name } } }",
            slug="aurora",
            q="properly",
        )

        assert [hit["identifier"] for hit in by_title["search"]["issues"]] == ["ENG-1"]

        by_identifier = await query(
            ada,
            "query Q($slug: String!, $q: String!) "
            "{ search(workspaceSlug: $slug, query: $q) "
            "{ issues { id identifier } } }",
            slug="aurora",
            q="ENG-1",
        )

        # First, because someone typing an identifier means that issue --
        # a text hit on the same string may follow it but must not displace it.
        assert by_identifier["search"]["issues"][0]["identifier"] == "ENG-1"

        # --- invite somebody ------------------------------------------------
        invitation = await mutate(
            ada,
            "mutation M($slug: String!, $email: String!) { invitationCreate(input: "
            "{workspaceSlug: $slug, email: $email, role: MEMBER}) "
            "{ invitation { id email role } token "
            "errors { field code message } } }",
            "invitationCreate",
            slug="aurora",
            email="grace@example.test",
        )

        # Returned once and never again: there is no query that reads it back,
        # only the digest is stored, and the raw string exists from here on
        # only in this variable and in whatever Ada pastes it into.
        assert invitation["token"]

        # --- a second person, in a second browser ----------------------------
        async with browser(application) as grace:
            grace_user = await sign_up(grace, "grace@example.test", name="Grace")

            # Nothing yet: registering is not joining.
            assert await query(grace, "query { myWorkspaces { role } }") == {
                "myWorkspaces": []
            }

            accepted = await mutate(
                grace,
                "mutation M($token: String!) { invitationAccept(input: "
                "{token: $token}) { workspace { id slug name } "
                "errors { field code message } } }",
                "invitationAccept",
                token=invitation["token"],
            )

            assert accepted["workspace"]["slug"] == "aurora"

            # Grace can now read the workspace, which is the acceptance
            # landing in `workspace_members` rather than merely in a payload.
            assert (
                await query(
                    grace,
                    "query Q($slug: String!) { issues(workspaceSlug: $slug) "
                    "{ totalCount } }",
                    slug="aurora",
                )
            )["issues"]["totalCount"] == 3

            # --- hand the issue over, and watch the inbox fill --------------
            #
            # This is the step that has to come AFTER the invitation, and not
            # because of ordering convenience: `notify` never writes to the
            # actor's own inbox -- by intent, by `IS DISTINCT FROM` in the
            # statement, and by `notifications_actor_is_not_recipient` in the
            # schema. A one-account workspace therefore cannot produce a
            # notification at all, and a test that expected one would be
            # asserting a bug.
            reassigned = await mutate(
                ada,
                "mutation M($slug: String!, $id: UUID!, $who: UUID!) "
                "{ issueUpdate(id: $id, input: {workspaceSlug: $slug, "
                "assigneeId: $who}) { issue { assigneeId } "
                "errors { field code message } } }",
                "issueUpdate",
                slug="aurora",
                id=issue["id"],
                who=grace_user["id"],
            )

            assert reassigned["issue"]["assigneeId"] == grace_user["id"]

            inbox = await query(
                grace,
                "query Q($slug: String!) { notificationUnreadCount("
                "workspaceSlug: $slug) "
                "notifications(workspaceSlug: $slug, unreadOnly: true) "
                "{ nodes { id kind actorId issueId readAt "
                "issue { identifier title } } } }",
                slug="aurora",
            )

            assert inbox["notificationUnreadCount"] == 1

            [notification] = inbox["notifications"]["nodes"]

            assert notification["kind"] == "ASSIGNED"
            assert notification["actorId"] == ada_user["id"]
            assert notification["issueId"] == issue["id"]
            assert notification["readAt"] is None

            # The issue is loaded per PAGE rather than per row, and it is the
            # only thing on a notification a person can actually read -- an
            # inbox of bare uuids is not an inbox.
            assert notification["issue"] == {
                "identifier": "ENG-1",
                "title": "Ship the thing, properly",
            }

            # Ada acted, so Ada has nothing: the same event seen from the
            # other side of the same workspace.
            assert (
                await query(
                    ada,
                    "query Q($slug: String!) "
                    "{ notificationUnreadCount(workspaceSlug: $slug) }",
                    slug="aurora",
                )
            )["notificationUnreadCount"] == 0

            marked = await mutate(
                grace,
                "mutation M($slug: String!, $id: UUID!) { notificationMarkRead("
                "input: {workspaceSlug: $slug, id: $id}) "
                "{ notification { id readAt } errors { field code message } } }",
                "notificationMarkRead",
                slug="aurora",
                id=notification["id"],
            )

            assert marked["notification"]["readAt"] is not None

        # --- the history the whole journey wrote ------------------------------
        #
        # Read last on purpose. Every mutation above recorded its own row
        # inside its own transaction, so this is the one assertion that can
        # only pass if all of them did -- and the one that would fail if a
        # feature wrote its change and forgot its history.
        history = await query(
            ada,
            "query Q($slug: String!, $id: UUID!) "
            "{ issue(workspaceSlug: $slug, id: $id) "
            "{ activity(first: 50) { nodes { kind actorId } } } }",
            slug="aurora",
            id=issue["id"],
        )

        kinds = {node["kind"] for node in history["issue"]["activity"]["nodes"]}

        assert kinds == {
            "CREATED",
            "TITLE_CHANGED",
            "PRIORITY_CHANGED",
            "STATE_CHANGED",
            "ASSIGNEE_CHANGED",
            "COMMENTED",
            "LABEL_ATTACHED",
            "RELATION_ADDED",
            "CYCLE_CHANGED",
            "PROJECT_CHANGED",
        }

        # --- log out, and stay out --------------------------------------------
        assert await query(ada, "mutation { logout { signedOut } }") == {
            "logout": {"signedOut": True}
        }

        # The jar is empty, so this client is now anonymous.
        assert ada.cookies.get(SESSION_COOKIE_NAME) is None
        assert await query(ada, "query { me { id } }") == {"me": None}

        # And the token itself is dead, which is the half a cleared cookie
        # cannot show: a stolen copy of the string presented by something
        # that never saw the Set-Cookie authenticates nobody.
        async with browser(application) as thief:
            thief.cookies.set(SESSION_COOKIE_NAME, ada_token, domain="vector.test")

            assert await query(thief, "query { me { id } }") == {"me": None}

        # --- log back in, and find everything where it was ----------------------
        assert (
            await mutate(
                ada,
                LOGIN,
                "login",
                email="ada@example.test",
                password=PASSWORD,
            )
        )["user"]["id"] == ada_user["id"]

        # A different session behind a different token, addressing the same
        # rows -- which is what makes this a durability check and not a
        # repeat of the reads above.
        assert session_token(ada) != ada_token

        restored = await query(
            ada,
            "query Q($slug: String!, $id: UUID!) "
            "{ issue(workspaceSlug: $slug, id: $id) "
            "{ identifier title description priority assigneeId creatorId "
            "workflowStateId projectId milestoneId "
            "cycle { name } project { name } labels { name } "
            "parent { identifier } "
            "children { nodes { identifier } } "
            "relations { nodes { type issue { identifier } } } "
            "comments { nodes { body } } } }",
            slug="aurora",
            id=issue["id"],
        )

        assert restored["issue"] == {
            "identifier": "ENG-1",
            "title": "Ship the thing, properly",
            "description": "With tests.",
            "priority": 2,
            "assigneeId": grace_user["id"],
            "creatorId": ada_user["id"],
            "workflowStateId": in_progress["id"],
            "projectId": project["id"],
            "milestoneId": milestone["id"],
            "cycle": {"name": "Sprint 1"},
            "project": {"name": "Launch"},
            "labels": [{"name": "urgent"}],
            # The issue is a parent, not a child.
            "parent": None,
            "children": {"nodes": [{"identifier": "ENG-2"}]},
            "relations": {"nodes": [{"type": "RELATED", "issue": {"identifier": "ENG-3"}}]},
            "comments": {"nodes": [{"body": "Starting on this."}]},
        }

        # `done` was resolved from the board and never used to move the issue.
        # Naming it here keeps the read above honest about which state the
        # issue is in: not the last one the test happened to look up.
        assert restored["issue"]["workflowStateId"] != done["id"]


# --------------------------------------------------------------------------
# Isolation, over the same transport
# --------------------------------------------------------------------------


@pytest.fixture
async def two_workspaces(application):
    """Two accounts, two workspaces, no overlap, built entirely over HTTP.

    Every id below was minted by the server for one tenant, and the second
    client has never been told any of them -- which is the situation a leaked
    id, a shared screenshot or a guessed uuid produces in the real world.
    """
    async with browser(application) as first, browser(application) as second:
        await sign_up(first, "first@example.test")
        await sign_up(second, "second@example.test")

        seeded = {}

        for client, slug, key in ((first, "aurora", "AUR"), (second, "borealis", "BOR")):
            await mutate(
                client,
                WORKSPACE_CREATE,
                "workspaceCreate",
                name=slug.title(),
                slug=slug,
            )

            team = (
                await mutate(
                    client,
                    TEAM_CREATE,
                    "teamCreate",
                    slug=slug,
                    name="Core",
                    key=key,
                )
            )["team"]

            issue = (
                await mutate(
                    client,
                    ISSUE_CREATE,
                    "issueCreate",
                    slug=slug,
                    team=team["id"],
                    title=f"{slug} secret",
                )
            )["issue"]

            label = (
                await mutate(
                    client,
                    "mutation M($slug: String!) { labelCreate(input: "
                    '{workspaceSlug: $slug, name: "internal"}) '
                    "{ label { id } errors { field code message } } }",
                    "labelCreate",
                    slug=slug,
                )
            )["label"]

            project = (
                await mutate(
                    client,
                    "mutation M($slug: String!) { projectCreate(input: "
                    '{workspaceSlug: $slug, name: "roadmap"}) '
                    "{ project { id } errors { field code message } } }",
                    "projectCreate",
                    slug=slug,
                )
            )["project"]

            seeded[slug] = {
                "team": team["id"],
                "issue": issue["id"],
                "label": label["id"],
                "project": project["id"],
            }

        yield first, second, seeded


# The operations the journey walks, each spelled to name a workspace and an
# id. Written out rather than derived from the schema, because the list is
# the claim: a field missing from here is a field nothing below covers.
CROSS_TENANT_OPERATIONS = {
    "issues": (
        "query Q($slug: String!) { issues(workspaceSlug: $slug) "
        "{ totalCount nodes { id title } } }"
    ),
    "issue": (
        "query Q($slug: String!, $id: UUID!) "
        "{ issue(workspaceSlug: $slug, id: $id) { id title } }"
    ),
    "search": (
        'query Q($slug: String!) { search(workspaceSlug: $slug, query: "secret") '
        "{ issues { id title } projects { id name } } }"
    ),
    "teams": "query Q($slug: String!) { teams(workspaceSlug: $slug) { id key } }",
    "notifications": (
        "query Q($slug: String!) { notifications(workspaceSlug: $slug) "
        "{ nodes { id } } }"
    ),
    "workspaceMembers": (
        "query Q($slug: String!) { workspaceMembers(workspaceSlug: $slug) "
        "{ userId email } }"
    ),
    "issueUpdate": (
        "mutation M($slug: String!, $id: UUID!) { issueUpdate(id: $id, "
        'input: {workspaceSlug: $slug, title: "seized"}) '
        "{ issue { id } errors { field code } } }"
    ),
    "commentCreate": (
        "mutation M($slug: String!, $id: UUID!) { commentCreate(input: "
        '{workspaceSlug: $slug, issueId: $id, body: "hello"}) '
        "{ comment { id } errors { field code } } }"
    ),
    "invitationCreate": (
        "mutation M($slug: String!) { invitationCreate(input: "
        '{workspaceSlug: $slug, email: "intruder@example.test", role: ADMIN}) '
        "{ invitation { id } token errors { field code } } }"
    ),
}


@pytest.mark.parametrize("operation", sorted(CROSS_TENANT_OPERATIONS))
async def test_a_second_workspace_reaches_none_of_the_first(operation, two_workspaces):
    """Naming someone else's workspace answers exactly as naming nothing does.

    Two requests, byte for byte compared. The refusal for a real workspace the
    caller is not in must be indistinguishable from the refusal for a slug no
    workspace holds -- otherwise a client walks a dictionary of slugs and
    learns which names are taken, one request at a time, and each hit is a
    company that uses this product.

    The `data` half of the comparison matters as much as the error: a refusal
    that still carried a row would be reporting what was reachable before the
    check ran.
    """
    _, second, seeded = two_workspaces

    document = CROSS_TENANT_OPERATIONS[operation]

    refused = await post(
        second, document, slug="aurora", id=seeded["aurora"]["issue"]
    )
    absent = await post(
        second,
        document,
        slug="no-such-workspace",
        id=seeded["aurora"]["issue"],
    )

    assert refused == absent

    [error] = refused["errors"]

    assert error["message"] == WORKSPACE_NOT_FOUND_MESSAGE
    assert error["extensions"] == {"code": "NOT_FOUND"}

    # Nothing came back. `issue` is nullable so its field error leaves a null
    # beside itself; every other field here is non-null and takes the whole
    # response down with it.
    assert refused["data"] in (None, {"issue": None})


@pytest.mark.parametrize("field", ["issue", "label", "project"])
async def test_another_workspaces_id_under_your_own_slug_is_simply_absent(
    field, two_workspaces
):
    """The other half: authorized for your own tenant, holding their id.

    Nothing refuses this -- the caller really is a member of the workspace
    they named. The predicate just matches no row, and the answer is the null
    an id that exists nowhere gets.
    """
    _, second, seeded = two_workspaces

    data = await query(
        second,
        f"query Q($slug: String!, $id: UUID!) "
        f"{{ {field}(workspaceSlug: $slug, id: $id) {{ id }} }}",
        slug="borealis",
        id=seeded["aurora"][field],
    )

    assert data == {field: None}


async def test_a_write_aimed_at_another_workspaces_issue_changes_nothing(
    two_workspaces,
):
    """A null cannot report this; only the row can.

    A resolver that answered NOT_FOUND while a statement had already written
    would satisfy every assertion above. So the update is sent under the
    caller's OWN slug -- authorized, unrefused, matching nothing -- and then
    the first account reads its issue back to say the title never moved.
    """
    first, second, seeded = two_workspaces

    payload = await query(
        second,
        CROSS_TENANT_OPERATIONS["issueUpdate"],
        slug="borealis",
        id=seeded["aurora"]["issue"],
    )

    assert payload["issueUpdate"]["issue"] is None
    assert payload["issueUpdate"]["errors"] == [{"field": "id", "code": "NOT_FOUND"}]

    survivor = await query(
        first,
        "query Q($slug: String!, $id: UUID!) "
        "{ issue(workspaceSlug: $slug, id: $id) { title } }",
        slug="aurora",
        id=seeded["aurora"]["issue"],
    )

    assert survivor["issue"]["title"] == "aurora secret"


# --------------------------------------------------------------------------
# The session, as a cookie rather than as a stub
# --------------------------------------------------------------------------


# Every protected shape in one place: the field that answers null for an
# anonymous caller, and a field that refuses one.
ANONYMOUS_PROBES = (
    "query { me { id } }",
    'query { issues(workspaceSlug: "aurora") { totalCount } }',
)


async def test_a_request_with_no_cookie_is_anonymous_everywhere(application):
    """No cookie, no identity -- and the refusal comes before the slug lookup.

    The slug named is a workspace that really exists and really has rows, so
    an UNAUTHENTICATED answer here proves the ORDER of the two checks rather
    than merely that both are present. Were it the other way round, the API
    would answer "no such workspace" to strangers and "not signed in" to
    strangers who guessed a real slug, which is an oracle for real slugs.
    """
    async with browser(application) as owner:
        await sign_up(owner, "owner@example.test")
        await mutate(
            owner, WORKSPACE_CREATE, "workspaceCreate", name="Aurora", slug="aurora"
        )

    async with browser(application) as stranger:
        assert await query(stranger, ANONYMOUS_PROBES[0]) == {"me": None}

        body = await post(stranger, ANONYMOUS_PROBES[1])

        [error] = body["errors"]

        assert error["message"] == UNAUTHENTICATED_MESSAGE
        assert error["extensions"] == {"code": "UNAUTHENTICATED"}
        assert body["data"] is None


async def test_one_session_carries_across_requests_and_dies_at_logout(application):
    """A session is a row, not a header: it outlives one request and is killed
    by name.

    Three claims, and the third is the one worth the round trips. The token is
    captured while it is live, presented again from a client that never saw
    the Set-Cookie -- so no cookie-clearing on the original client can be what
    makes it fail -- and answers nobody. That is the server-side delete, which
    is the only kind of log-out that means anything: clearing a cookie asks
    the browser to forget a token that would otherwise still work.
    """
    async with browser(application) as user:
        account = await sign_up(user, "sam@example.test", name="Sam")
        token = session_token(user)

        await mutate(
            user, WORKSPACE_CREATE, "workspaceCreate", name="Aurora", slug="aurora"
        )

        # Same cookie, three requests, one identity.
        for _ in range(3):
            assert await query(user, "query { me { id } }") == {
                "me": {"id": account["id"]}
            }

        await query(user, "mutation { logout { signedOut } }")

    async with browser(application) as replay:
        replay.cookies.set(SESSION_COOKIE_NAME, token, domain="vector.test")

        assert await query(replay, "query { me { id } }") == {"me": None}

        body = await post(replay, ANONYMOUS_PROBES[1])

        assert body["errors"][0]["extensions"] == {"code": "UNAUTHENTICATED"}


# --------------------------------------------------------------------------
# Paging a real list through the schema
# --------------------------------------------------------------------------


# Enough rows to need several pages at a page size that divides none of them
# evenly, and priorities that repeat so the sort has real ties for the id to
# break. The second team exists only to be excluded: a filtered walk that
# quietly ignored its filter would still return every row exactly once, and
# would still pass a test that did not have rows to leave out.
PAGED_ISSUES = 13
PAGE_SIZE = 4
PRIORITIES = (0, 1, 2, 3)


@pytest.fixture
async def paged_workspace(application):
    """A workspace holding a multi-page list, and a second team beside it."""
    async with browser(application) as client:
        await sign_up(client, "pager@example.test")
        await mutate(
            client, WORKSPACE_CREATE, "workspaceCreate", name="Aurora", slug="aurora"
        )

        teams = {}

        for name, key in (("Engineering", "ENG"), ("Design", "DES")):
            teams[key] = (
                await mutate(
                    client, TEAM_CREATE, "teamCreate", slug="aurora", name=name, key=key
                )
            )["team"]["id"]

        wanted = []

        for index in range(PAGED_ISSUES):
            issue = (
                await mutate(
                    client,
                    "mutation M($slug: String!, $team: UUID!, $title: String!, "
                    "$priority: Int!) { issueCreate(input: {workspaceSlug: $slug, "
                    "teamId: $team, title: $title, priority: $priority}) "
                    "{ issue { id identifier priority } "
                    "errors { field code message } } }",
                    "issueCreate",
                    slug="aurora",
                    team=teams["ENG"],
                    title=f"Engineering {index}",
                    priority=PRIORITIES[index % len(PRIORITIES)],
                )
            )["issue"]

            wanted.append(issue["id"])

        # The rows the filter must leave out, at the same priorities.
        for index in range(5):
            await mutate(
                client,
                "mutation M($slug: String!, $team: UUID!, $title: String!, "
                "$priority: Int!) { issueCreate(input: {workspaceSlug: $slug, "
                "teamId: $team, title: $title, priority: $priority}) "
                "{ issue { id } errors { field code message } } }",
                "issueCreate",
                slug="aurora",
                team=teams["DES"],
                title=f"Design {index}",
                priority=PRIORITIES[index % len(PRIORITIES)],
            )

        yield client, teams, wanted


PAGE = """
query Page($slug: String!, $team: UUID!, $order: IssueOrderInput!,
           $first: Int!, $after: String) {
  issues(workspaceSlug: $slug, filter: {teamId: $team}, orderBy: $order,
         first: $first, after: $after) {
    totalCount
    nodes { id priority }
    pageInfo { hasNextPage endCursor }
  }
}
"""

ORDER = {"field": "PRIORITY", "direction": "ASC"}


async def test_a_filtered_walk_in_a_non_default_order_returns_every_row_once(
    paged_workspace,
):
    """Walk the whole list four at a time and land on exactly the same rows.

    Paged through the SCHEMA rather than the service, which is the seam:
    `tests/test_issue_filters_db.py` proves the walk against
    `IssueService.list` with a domain `IssueOrder`, and everything between
    that and a client -- the `IssueOrderInput` translation, the cursor
    surviving a JSON round trip, the filter travelling to `totalCount` as
    well as to the page -- is untested until something drives it over HTTP.

    Three claims, and the third is the one that catches a keyset bug: every
    row appears, no row appears twice, and the paged sequence is the same
    sequence an unpaged read returns. A predicate that skipped a tie or served
    one twice satisfies the first two counts and fails the third.
    """
    client, teams, wanted = paged_workspace

    walked = []
    cursors = []
    after = None

    # Bounded: PAGED_ISSUES // PAGE_SIZE + 2 is one more request than the walk
    # can legally need, so a cursor that never advances fails here instead of
    # looping forever.
    for _ in range(PAGED_ISSUES // PAGE_SIZE + 2):
        page = (
            await query(
                client,
                PAGE,
                slug="aurora",
                team=teams["ENG"],
                order=ORDER,
                first=PAGE_SIZE,
                after=after,
            )
        )["issues"]

        # The count is the whole match and never the page, on every page.
        assert page["totalCount"] == PAGED_ISSUES

        walked.extend(node["id"] for node in page["nodes"])
        cursors.append(page["pageInfo"]["endCursor"])

        if not page["pageInfo"]["hasNextPage"]:
            break

        after = page["pageInfo"]["endCursor"]
    else:
        raise AssertionError("the walk never reported a last page")

    assert len(walked) == PAGED_ISSUES
    assert len(set(walked)) == PAGED_ISSUES
    assert set(walked) == set(wanted)

    # Against one unpaged read of the same filter and the same order, which is
    # what says the pages were cut out of the right sequence rather than
    # merely covering it.
    whole = (
        await query(
            client,
            PAGE,
            slug="aurora",
            team=teams["ENG"],
            order=ORDER,
            first=PAGED_ISSUES,
            after=None,
        )
    )["issues"]

    assert walked == [node["id"] for node in whole["nodes"]]

    # And it really was sorted by priority, not merely returned whole.
    assert [node["priority"] for node in whole["nodes"]] == sorted(
        node["priority"] for node in whole["nodes"]
    )


async def test_a_cursor_cannot_be_replayed_under_a_different_sort(paged_workspace):
    """The cursor carries its ordering, and the server refuses the mismatch.

    This is the failure that does not announce itself. Resuming a keyset walk
    in a different sort does not error at the database: the predicate compares
    the wrong column against the wrong value and returns a page, made of rows
    the client has already seen or rows it never will. So the refusal is the
    feature.

    ORDER_MISMATCH rather than INVALID_CURSOR, because the client sent exactly
    what the previous page handed it and the fix is different: start again
    without the cursor, rather than stop sending garbage.
    """
    client, teams, _ = paged_workspace

    first_page = (
        await query(
            client,
            PAGE,
            slug="aurora",
            team=teams["ENG"],
            order=ORDER,
            first=PAGE_SIZE,
            after=None,
        )
    )["issues"]

    cursor = first_page["pageInfo"]["endCursor"]

    assert cursor

    body = await post(
        client,
        PAGE,
        slug="aurora",
        team=teams["ENG"],
        order={"field": "CREATED_AT", "direction": "DESC"},
        first=PAGE_SIZE,
        after=cursor,
    )

    [error] = body["errors"]

    # A query field has no payload to put a rejection in, so it travels as a
    # top-level error -- which is masked unless it carries a published code.
    assert error["extensions"]["code"] == "BAD_USER_INPUT"
    assert [issue["code"] for issue in error["extensions"]["issues"]] == [
        "ORDER_MISMATCH"
    ]
    assert body["data"] is None

    # The same cursor under the order it was minted for still works, so the
    # refusal above is about the ordering and not about the cursor.
    resumed = (
        await query(
            client,
            PAGE,
            slug="aurora",
            team=teams["ENG"],
            order=ORDER,
            first=PAGE_SIZE,
            after=cursor,
        )
    )["issues"]

    already_seen = {node["id"] for node in first_page["nodes"]}

    assert already_seen.isdisjoint(node["id"] for node in resumed["nodes"])


# --------------------------------------------------------------------------
# The chain that has to run before any of the above can
# --------------------------------------------------------------------------


async def test_the_migration_chain_applies_from_an_empty_database_in_order(
    postgres_dsn,
):
    """001 through 015, through the real runner, onto nothing.

    Every db suite here applies the chain, so "it runs" is well covered. What
    is not is the LEDGER the runner leaves behind -- and the ledger is what a
    deployment consults to decide what to do next. A migration applied without
    its row would be re-applied on the next deploy; a row written with the
    wrong checksum would make the runner refuse to start ever again.

    The second pass is the other half. Deploys re-run this command, so
    applying an already-applied chain has to be a no-op rather than an error
    or a duplicate row.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)

        # Nothing at all to begin with -- not even the runner's own ledger,
        # which is the state a brand-new database is in and the one the
        # bootstrap path has to survive.
        assert (
            await connection.fetchval("SELECT to_regclass('public.schema_migrations')")
            is None
        )

        applied = await apply_all_migrations(connection)

        files = sorted(MIGRATIONS_DIR.glob("*.sql"))

        assert applied == [path.name for path in files]

        ledger = await connection.fetch(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        )

        assert [(row["version"], row["checksum"]) for row in ledger] == [
            (parse_version(path), compute_checksum(path.read_text(encoding="utf-8")))
            for path in files
        ]

        # A second full pass writes nothing and raises nothing.
        assert await apply_all_migrations(connection) == [
            path.name for path in files
        ]
        assert (
            await connection.fetchval("SELECT count(*) FROM schema_migrations")
            == len(files)
        )
    finally:
        await connection.close()
