"""What a member of one workspace can reach in another. Nothing.

Every other tenancy suite here asks the question one layer at a time: does
the repository's statement carry a workspace predicate, does the service pass
the scope it was given, does the resolver forward it. All of that can be true
of a schema in which the workspace is a constant -- and until this change it
was, because `app/graphql/tenancy.py` resolved one hardcoded slug for every
request. A suite of green tenancy tests coexisted with an API that had
exactly one tenant.

So this file asks the question the other way round, through the real schema,
against a real PostgreSQL, with two workspaces and a user who belongs to one
of them:

  * naming the workspace the viewer is NOT in answers NOT_FOUND, with one
    fixed message, and returns no data -- for issues, labels, cycles,
    projects, teams, comments and relations alike;
  * an id belonging to the OTHER workspace, presented with the viewer's own
    slug, resolves to null or NOT_FOUND rather than to the row -- and the
    row is still there afterwards, unchanged, which is the half a null
    cannot report;
  * a team id from the other workspace leaks neither its cycles nor its
    issues, and cannot be filed against;
  * an unauthenticated caller is refused before any workspace is looked up
    at all, so the API is not an oracle for which slugs are taken.

The refusals are deliberately indistinguishable from the answers a caller
gets for things that do not exist. That is the property under test: a client
holding a guessed id and a slug they may not use must not be able to tell
"not yours" from "not there", or the API becomes a way to enumerate other
tenants one request at a time.

The viewer is stubbed on the context and everything below it is real -- the
schema, the resolvers, the services, the repositories, the membership lookup
and the server. A session cookie needs an HTTP request, which
`Schema.execute` has none of; the membership row the whole check turns on is
a real row in `workspace_members`.

Marked `db`: deselected by default, skipped when Docker is unreachable.
Nothing here touches DATABASE_URL or Neon.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import asyncpg
import pytest

from app.domain.relations import RelationType
from app.domain.tenancy import WorkspaceScope
from app.graphql.schema import build_schema
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE
from app.repositories.comments import CommentRepository
from app.repositories.cycles import CycleRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.issues import IssueRepository
from app.repositories.labels import LabelRepository
from app.repositories.memberships import MembershipRepository
from app.repositories.projects import ProjectRepository
from app.repositories.relations import RelationRepository
from app.repositories.teams import TeamRepository
from app.services.comments import CommentService
from app.services.cycles import CycleService
from app.services.issues import IssueService
from app.services.labels import LabelService
from app.services.memberships import MembershipService
from app.services.projects import ProjectService
from app.services.relations import RelationService
from app.services.teams import TeamService

from tests.conftest import (
    apply_all_migrations,
    graphql_context,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

schema = build_schema("test")

# Two tenants, neither of them the one 002 seeds. Using the bootstrap
# workspace for either half would make "the viewer's workspace" and "the
# workspace the old code hardcoded" the same row, and a resolver that had
# quietly kept a default would still pass.
WORKSPACE_A = UUID("00000000-0000-7000-8000-0000000000a0")
TEAM_A = UUID("00000000-0000-7000-8000-0000000000a1")
SLUG_A = "alpha"

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000b0")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000b1")
SLUG_B = "beta"

# The viewer for every test below: a member of A, and of nothing else.
USER_A = UUID("00000000-0000-7000-8000-0000000000a9")

# A member of B, who exists only so that B can hold a comment -- authorship
# is a foreign key against B's own membership, so B's data cannot be seeded
# without one. No test authenticates as this user.
USER_B = UUID("00000000-0000-7000-8000-0000000000b9")

# A slug no workspace holds. The comparison that matters is between the
# answer for this and the answer for `beta`: if they differ in any way a
# client can see, the difference reports that `beta` exists.
ABSENT_SLUG = "no-such-workspace"

INSERT_WORKSPACE = "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)"
INSERT_TEAM = "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)"
INSERT_USER = """
INSERT INTO users (id, email, password_hash)
VALUES ($1, $2, '$argon2id$not-a-real-hash')
"""
INSERT_MEMBERSHIP = """
INSERT INTO workspace_members (workspace_id, user_id, role)
VALUES ($1, $2, 'member')
"""


@pytest.fixture
async def world(postgres_dsn):
    """Two workspaces with a full set of rows each, and the real services.

    Seeded through the services rather than by hand wherever a service
    exists, so the rows under test are the rows the product writes. What is
    inserted directly is only what the API has no mutation for yet:
    workspaces, teams, users and memberships.

    Yields the context a resolver will see, the connection to read rows back
    over, and the ids the tests assert against. The context's viewer is
    USER_A in every test; a test that wants an anonymous caller replaces it.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)

        for workspace_id, slug, name, team_id, key, user_id in (
            (WORKSPACE_A, SLUG_A, "Alpha", TEAM_A, "ALPHA", USER_A),
            (WORKSPACE_B, SLUG_B, "Beta", TEAM_B, "BETA", USER_B),
        ):
            await connection.execute(INSERT_WORKSPACE, workspace_id, slug, name)
            await connection.execute(INSERT_TEAM, team_id, workspace_id, name, key)
            await seed_workflow_states(connection, workspace_id, team_id)
            await connection.execute(INSERT_USER, user_id, f"{slug}@example.test")
            await connection.execute(INSERT_MEMBERSHIP, workspace_id, user_id)

        pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=4)

        try:
            team_service = TeamService(pool=pool, repository=TeamRepository())
            issue_service = IssueService(
                pool=pool, repository=IssueRepository(), teams=team_service
            )
            label_service = LabelService(
                pool=pool,
                repository=LabelRepository(),
                issue_label_repository=IssueLabelRepository(),
            )
            cycle_service = CycleService(pool=pool, repository=CycleRepository())
            project_service = ProjectService(
                pool=pool,
                repository=ProjectRepository(),
                issue_repository=IssueRepository(),
            )
            comment_service = CommentService(pool=pool, repository=CommentRepository())
            relation_service = RelationService(
                pool=pool, repository=RelationRepository()
            )

            seeded = {}

            for key, workspace_id, team_id, user_id in (
                ("a", WORKSPACE_A, TEAM_A, USER_A),
                ("b", WORKSPACE_B, TEAM_B, USER_B),
            ):
                scope = WorkspaceScope(workspace_id=workspace_id)

                issue = await issue_service.create(
                    scope=scope,
                    team_id=team_id,
                    title=f"{key} issue",
                    description=None,
                    priority=0,
                    creator_id=user_id,
                )
                other = await issue_service.create(
                    scope=scope,
                    team_id=team_id,
                    title=f"{key} second issue",
                    description=None,
                    priority=0,
                    creator_id=user_id,
                )
                label = await label_service.create(
                    scope=scope, name=f"{key}-label", color="#112233"
                )
                cycle = await cycle_service.create(
                    scope=scope,
                    team_id=team_id,
                    number=1,
                    name=f"{key} cycle",
                    starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    ends_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
                )
                project = await project_service.create(
                    scope=scope,
                    name=f"{key} project",
                    description=None,
                    state="planned",
                    target_date=date(2026, 6, 1),
                )
                comment = await comment_service.create(
                    scope=scope,
                    issue_id=issue.id,
                    author_id=user_id,
                    body=f"{key} comment",
                )
                relation = await relation_service.create_relation(
                    scope=scope,
                    source_issue_id=issue.id,
                    target_issue_id=other.id,
                    relation_type=RelationType.RELATED,
                )

                seeded[key] = SimpleNamespace(
                    issue=issue.id,
                    other_issue=other.id,
                    label=label.id,
                    cycle=cycle.id,
                    project=project.id,
                    comment=comment.id,
                    relation=relation.id,
                )

            context = graphql_context(
                issue_service=issue_service,
                team_service=team_service,
                label_service=label_service,
                cycle_service=cycle_service,
                project_service=project_service,
                comment_service=comment_service,
                relation_service=relation_service,
                # The real one, over the real table. This is the lookup the
                # whole file is about, so it is the one thing that must not
                # be a fake: the refusals below come from a missing row in
                # `workspace_members` and not from a stub deciding to raise.
                membership_service=MembershipService(
                    pool=pool, repository=MembershipRepository()
                ),
            )

            async def viewer():
                return SimpleNamespace(id=USER_A)

            context.viewer = viewer

            yield context, connection, seeded
        finally:
            await pool.close()
    finally:
        await connection.close()


async def run(context, document, **variables):
    return await schema.execute(
        document, variable_values=variables or None, context_value=context
    )


def assert_refused(result, message, code, field=None):
    """One error carrying `message`/`code`, and no row anywhere in `data`.

    `data` is asserted as well as the error, because a refusal that still
    carried a partial result would be reporting what was reachable before the
    check ran. Two shapes are legal for that, decided by the field's own
    nullability and not by the resolver: a NON-NULL field propagates the
    error up to the root and `data` is None, while a NULLABLE one -- `issue`,
    `label`, `cycle`, `project` -- is set to null and the error travels beside
    it. Either way the row is absent, which is the claim; `field` says which
    of the two to expect so that neither is accepted by accident.
    """
    assert result.errors is not None
    assert len(result.errors) == 1

    formatted = result.errors[0].formatted

    assert formatted["message"] == message
    assert formatted["extensions"] == {"code": code}

    assert result.data == (None if field is None else {field: None})


# The reads whose GraphQL type is nullable, so a field error leaves a null in
# `data` instead of nulling the whole response.
NULLABLE_READS = frozenset({"issue", "label", "cycle", "project"})


def assert_workspace_not_found(result, field=None):
    """The one answer every cross-workspace request gets."""
    assert_refused(result, WORKSPACE_NOT_FOUND_MESSAGE, "NOT_FOUND", field)


# --------------------------------------------------------------------------
# Reads: naming a workspace the viewer is not in
# --------------------------------------------------------------------------


# One entry per workspace-scoped root read. Written out rather than derived
# from the schema, because the list is the claim: a field added without a
# workspace argument is a field this file does not cover, and a reviewer
# comparing this table with `type Query` is what catches it.
SCOPED_QUERIES = {
    "issues": "query Q($slug: String!) { issues(workspaceSlug: $slug) { nodes { id } } }",
    "issue": (
        "query Q($slug: String!, $id: UUID!) "
        "{ issue(workspaceSlug: $slug, id: $id) { id } }"
    ),
    "labels": "query Q($slug: String!) { labels(workspaceSlug: $slug) { nodes { id } } }",
    "label": (
        "query Q($slug: String!, $id: UUID!) "
        "{ label(workspaceSlug: $slug, id: $id) { id } }"
    ),
    "cycles": (
        "query Q($slug: String!, $team: UUID!) "
        "{ cycles(workspaceSlug: $slug, teamId: $team) { id } }"
    ),
    "cycle": (
        "query Q($slug: String!, $id: UUID!) "
        "{ cycle(workspaceSlug: $slug, id: $id) { id } }"
    ),
    "projects": (
        "query Q($slug: String!) { projects(workspaceSlug: $slug) { nodes { id } } }"
    ),
    "project": (
        "query Q($slug: String!, $id: UUID!) "
        "{ project(workspaceSlug: $slug, id: $id) { id } }"
    ),
    "teams": "query Q($slug: String!) { teams(workspaceSlug: $slug) { id } }",
}


@pytest.mark.parametrize("field", sorted(SCOPED_QUERIES))
async def test_reading_another_workspace_is_not_found_and_returns_no_data(field, world):
    """The headline. A member of A naming B gets nothing, from every read.

    Parametrised over the whole read surface rather than asserted for one
    field, because the failure this guards against is per resolver: the check
    is one helper, but a resolver that forgot to call it would be the only
    one that leaked, and a single-field test would have a nine-in-ten chance
    of not being the one that noticed.
    """
    context, _, seeded = world

    result = await run(
        context,
        SCOPED_QUERIES[field],
        slug=SLUG_B,
        id=str(seeded["b"].issue),
        team=str(TEAM_B),
    )

    assert_workspace_not_found(result, field if field in NULLABLE_READS else None)


@pytest.mark.parametrize("field", sorted(SCOPED_QUERIES))
async def test_a_workspace_that_does_not_exist_answers_identically(field, world):
    """ "Not yours" and "not there" are the same response, byte for byte.

    This is what makes the refusal above non-disclosing. If the two differed
    -- a different code, a different message, an error in one case and an
    empty list in the other -- a caller could walk a dictionary of slugs and
    learn which ones are real workspaces they are simply not in.
    """
    context, _, seeded = world

    refused = await run(
        context,
        SCOPED_QUERIES[field],
        slug=SLUG_B,
        id=str(seeded["b"].issue),
        team=str(TEAM_B),
    )
    absent = await run(
        context,
        SCOPED_QUERIES[field],
        slug=ABSENT_SLUG,
        id=str(seeded["b"].issue),
        team=str(TEAM_B),
    )

    assert_workspace_not_found(absent, field if field in NULLABLE_READS else None)
    assert absent.data == refused.data
    assert [error.formatted for error in absent.errors] == [
        error.formatted for error in refused.errors
    ]


@pytest.mark.parametrize("field", sorted(SCOPED_QUERIES))
async def test_an_unauthenticated_caller_is_refused_before_the_slug_is_looked_up(
    field, world
):
    """No identity, no lookup -- so this is not an oracle for real slugs.

    The viewer's own slug is used deliberately: it is a workspace that exists
    and has rows. An anonymous caller still gets UNAUTHENTICATED and never
    the membership refusal, which is what proves the order of the two checks
    rather than merely that both exist.
    """
    context, _, seeded = world

    async def anonymous():
        return None

    context.viewer = anonymous

    result = await run(
        context,
        SCOPED_QUERIES[field],
        slug=SLUG_A,
        id=str(seeded["a"].issue),
        team=str(TEAM_A),
    )

    assert_refused(
        result,
        UNAUTHENTICATED_MESSAGE,
        "UNAUTHENTICATED",
        field if field in NULLABLE_READS else None,
    )


async def test_the_viewers_own_workspace_still_answers_with_its_own_rows(world):
    """The control, without which every assertion above passes vacuously.

    A schema that refused everything would satisfy this whole file except
    here. It also pins the positive half of the isolation claim: A's page
    holds A's issues and none of B's, from the same request shape that was
    refused for B.
    """
    context, _, seeded = world

    result = await run(context, SCOPED_QUERIES["issues"], slug=SLUG_A)

    assert result.errors is None

    ids = {node["id"] for node in result.data["issues"]["nodes"]}

    assert ids == {str(seeded["a"].issue), str(seeded["a"].other_issue)}
    assert str(seeded["b"].issue) not in ids
    assert str(seeded["b"].other_issue) not in ids


# --------------------------------------------------------------------------
# Reads: the other workspace's id, presented with the viewer's own slug
# --------------------------------------------------------------------------


# (field, document), where the document reads one row by id in a workspace.
# The id passed is always B's and the slug is always A's, which is the attack
# a leaked or guessed id makes possible.
BY_ID_QUERIES = {
    "issue": SCOPED_QUERIES["issue"],
    "label": SCOPED_QUERIES["label"],
    "cycle": SCOPED_QUERIES["cycle"],
    "project": SCOPED_QUERIES["project"],
}


@pytest.mark.parametrize("field", sorted(BY_ID_QUERIES))
async def test_another_workspaces_id_under_the_viewers_own_slug_resolves_to_null(
    field, world
):
    """A real id the caller may not see answers as an id that exists nowhere.

    Null and not an error, because these fields are nullable and "no such row
    here" is an ordinary answer. What must not happen is the row coming back,
    and what must equally not happen is a distinguishable failure -- either
    one turns a guessed uuid into a membership test for another tenant.
    """
    context, _, seeded = world

    result = await run(
        context,
        BY_ID_QUERIES[field],
        slug=SLUG_A,
        id=str(getattr(seeded["b"], field)),
    )

    assert result.errors is None
    assert result.data == {field: None}


async def test_another_teams_cycles_are_not_reachable_through_the_viewers_workspace(
    world,
):
    """`cycles(teamId:)` takes a client-supplied team id, so it is the field
    most obviously shaped like a leak: name your own workspace, name someone
    else's team.

    The statement is scoped by workspace AND team, so the two intersect in
    nothing and the answer is the empty list an unknown team id gets. An
    error here would be worse than useless -- it would confirm the team.
    """
    context, _, _ = world

    result = await run(context, SCOPED_QUERIES["cycles"], slug=SLUG_A, team=str(TEAM_B))

    assert result.errors is None
    assert result.data == {"cycles": []}


async def test_the_team_filter_on_issues_narrows_and_never_widens(world):
    """The same shape on `issues`, where the filter is optional.

    Two claims in one: A's own team filters A's page down to A's issues, and
    B's team filters it to nothing rather than to B's.
    """
    context, _, seeded = world

    document = (
        "query Q($slug: String!, $team: UUID!) "
        "{ issues(workspaceSlug: $slug, teamId: $team) { nodes { id } } }"
    )

    own = await run(context, document, slug=SLUG_A, team=str(TEAM_A))
    foreign = await run(context, document, slug=SLUG_A, team=str(TEAM_B))

    assert own.errors is None
    assert {node["id"] for node in own.data["issues"]["nodes"]} == {
        str(seeded["a"].issue),
        str(seeded["a"].other_issue),
    }

    assert foreign.errors is None
    assert foreign.data == {"issues": {"nodes": []}}


async def test_a_comment_thread_in_another_workspace_is_unreachable(world):
    """`Issue.comments` is nested, so it inherits its parent's scope.

    Reached the only way a client can reach it -- through an issue -- so the
    refusal happens at the issue and the thread is never resolved. B's
    comment exists; this is what a caller in A sees of it.
    """
    context, _, seeded = world

    document = (
        "query Q($slug: String!, $id: UUID!) "
        "{ issue(workspaceSlug: $slug, id: $id) "
        "{ id comments { nodes { id body } } } }"
    )

    result = await run(context, document, slug=SLUG_A, id=str(seeded["b"].issue))

    assert result.errors is None
    assert result.data == {"issue": None}


async def test_relations_of_another_workspaces_issue_are_unreachable(world):
    """Same again for `Issue.relations`, which B genuinely has one of."""
    context, _, seeded = world

    document = (
        "query Q($slug: String!, $id: UUID!) "
        "{ issue(workspaceSlug: $slug, id: $id) "
        "{ id relations { nodes { id type } } } }"
    )

    refused = await run(context, document, slug=SLUG_A, id=str(seeded["b"].issue))
    own = await run(context, document, slug=SLUG_A, id=str(seeded["a"].issue))

    assert refused.errors is None
    assert refused.data == {"issue": None}

    # The control: A's own issue does have a relation, so the null above is
    # the scope refusing and not the field being empty for everyone.
    assert own.errors is None
    assert [node["id"] for node in own.data["issue"]["relations"]["nodes"]] == [
        str(seeded["a"].relation)
    ]


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


# One entry per workspace-scoped mutation whose input carries the slug, with
# the variables that would otherwise succeed. `issueArchive` and `cycleDelete`
# are covered separately because they spell the slug as a field argument.
SCOPED_MUTATIONS = {
    "issueUpdate": (
        "mutation M($slug: String!, $id: UUID!) "
        '{ issueUpdate(id: $id, input: {workspaceSlug: $slug, title: "seized"}) '
        "{ issue { id } errors { field code } } }"
    ),
    "labelCreate": (
        "mutation M($slug: String!) "
        '{ labelCreate(input: {workspaceSlug: $slug, name: "intruder"}) '
        "{ label { id } errors { field code } } }"
    ),
    "commentCreate": (
        "mutation M($slug: String!, $id: UUID!) "
        "{ commentCreate(input: "
        '{workspaceSlug: $slug, issueId: $id, body: "intruder"}) '
        "{ comment { id } errors { field code } } }"
    ),
    "cycleCreate": (
        "mutation M($slug: String!, $team: UUID!) "
        "{ cycleCreate(input: {workspaceSlug: $slug, teamId: $team, number: 99, "
        'startsAt: "2027-01-01T00:00:00+00:00", '
        'endsAt: "2027-01-15T00:00:00+00:00"}) '
        "{ cycle { id } errors { field code } } }"
    ),
    "projectCreate": (
        "mutation M($slug: String!) "
        '{ projectCreate(input: {workspaceSlug: $slug, name: "intruder"}) '
        "{ project { id } errors { field code } } }"
    ),
    "issueRelationCreate": (
        "mutation M($slug: String!, $id: UUID!, $other: UUID!) "
        "{ issueRelationCreate(input: {workspaceSlug: $slug, sourceIssueId: $id, "
        "targetIssueId: $other, type: RELATED}) "
        "{ relation { id } errors { field code } } }"
    ),
    "issueSetParent": (
        "mutation M($slug: String!, $id: UUID!, $other: UUID!) "
        "{ issueSetParent(input: {workspaceSlug: $slug, issueId: $id, "
        "parentId: $other}) { issue { id } errors { field code } } }"
    ),
}


@pytest.mark.parametrize("field", sorted(SCOPED_MUTATIONS))
async def test_writing_into_another_workspace_is_not_found(field, world):
    """Every write refuses the same way every read does.

    Refused as a top-level NOT_FOUND rather than as a field error inside the
    payload, and the distinction is deliberate: a payload error says the
    client sent something it could correct, and there is nothing to correct
    about a workspace the caller may not act in.
    """
    context, _, seeded = world

    result = await run(
        context,
        SCOPED_MUTATIONS[field],
        slug=SLUG_B,
        id=str(seeded["b"].issue),
        other=str(seeded["b"].other_issue),
        team=str(TEAM_B),
    )

    assert_workspace_not_found(result)


async def test_archiving_and_deleting_refuse_through_the_field_argument_too(world):
    """The two mutations that spell `workspaceSlug` beside their id.

    Worth their own test rather than the table above precisely because their
    shape differs: a slug read from the wrong place is exactly the kind of
    mistake that survives a table keyed on input objects.
    """
    context, _, seeded = world

    archive = await run(
        context,
        "mutation M($slug: String!, $id: UUID!) "
        "{ issueArchive(workspaceSlug: $slug, id: $id) "
        "{ issue { id } errors { field code } } }",
        slug=SLUG_B,
        id=str(seeded["b"].issue),
    )
    delete = await run(
        context,
        "mutation M($slug: String!, $id: UUID!) "
        "{ cycleDelete(workspaceSlug: $slug, id: $id) "
        "{ deletedCycleId errors { field code } } }",
        slug=SLUG_B,
        id=str(seeded["b"].cycle),
    )

    assert_workspace_not_found(archive)
    assert_workspace_not_found(delete)


async def test_another_workspaces_issue_survives_an_update_sent_under_our_own_slug(
    world,
):
    """The strongest form of the claim, and the one a null cannot make.

    An id from B with A's slug is authorized -- the caller really is a member
    of A -- so nothing refuses the request outright. The UPDATE simply
    matches no row, because the workspace is in its predicate. Reported as
    NOT_FOUND on the payload, which is the same answer an id that exists
    nowhere gets.

    Then the row is read back over SQL. A resolver that returned NOT_FOUND
    while a statement had already written would satisfy every assertion above
    it; only the row can say the write did not happen.
    """
    context, connection, seeded = world

    result = await run(
        context,
        SCOPED_MUTATIONS["issueUpdate"],
        slug=SLUG_A,
        id=str(seeded["b"].issue),
    )

    assert result.errors is None
    assert result.data["issueUpdate"]["issue"] is None
    assert result.data["issueUpdate"]["errors"] == [
        {"field": "id", "code": "NOT_FOUND"}
    ]

    row = await connection.fetchrow(
        "SELECT title, workspace_id FROM issues WHERE id = $1", seeded["b"].issue
    )

    assert row["title"] == "b issue"
    assert row["workspace_id"] == WORKSPACE_B


async def test_another_workspaces_issue_survives_an_archive_sent_under_our_own_slug(
    world,
):
    """The same, for the write whose effect is invisible in a later read.

    An archived issue is absent from every query, so a successful archive of
    B's issue would look exactly like the correct refusal from A's side. Only
    `archived_at` distinguishes them.
    """
    context, connection, seeded = world

    result = await run(
        context,
        "mutation M($slug: String!, $id: UUID!) "
        "{ issueArchive(workspaceSlug: $slug, id: $id) "
        "{ issue { id } errors { field code } } }",
        slug=SLUG_A,
        id=str(seeded["b"].issue),
    )

    assert result.errors is None
    assert result.data["issueArchive"]["issue"] is None

    archived_at = await connection.fetchval(
        "SELECT archived_at FROM issues WHERE id = $1", seeded["b"].issue
    )

    assert archived_at is None


async def test_a_comment_cannot_be_written_onto_another_workspaces_issue(world):
    """Authorized in A, aimed at B's issue. The issue is not in A, so there is
    nothing to comment on -- reported as the same NOT_FOUND an unknown issue
    id gets, and no row is written."""
    context, connection, seeded = world

    result = await run(
        context,
        SCOPED_MUTATIONS["commentCreate"],
        slug=SLUG_A,
        id=str(seeded["b"].issue),
    )

    assert result.errors is None
    assert result.data["commentCreate"]["comment"] is None
    assert result.data["commentCreate"]["errors"] != []

    count = await connection.fetchval(
        "SELECT count(*) FROM comments WHERE issue_id = $1", seeded["b"].issue
    )

    # Only the one the fixture seeded.
    assert count == 1


async def test_a_relation_cannot_be_drawn_to_another_workspaces_issue(world):
    """A's issue as the source, B's as the target, under A's own slug.

    The relation service resolves both ends inside one workspace, so the
    target is simply not there. Nothing is written, and the answer does not
    say which of the two ids was the problem -- saying so would confirm B's.
    """
    context, connection, seeded = world

    result = await run(
        context,
        SCOPED_MUTATIONS["issueRelationCreate"],
        slug=SLUG_A,
        id=str(seeded["a"].issue),
        other=str(seeded["b"].issue),
    )

    assert result.errors is None
    assert result.data["issueRelationCreate"]["relation"] is None
    assert result.data["issueRelationCreate"]["errors"] != []

    crossing = await connection.fetchval(
        """
        SELECT count(*)
        FROM issue_relations
        WHERE source_issue_id = $1 OR target_issue_id = $1
        """,
        seeded["b"].issue,
    )

    # Only the relation the fixture drew inside B.
    assert crossing == 1


async def test_an_issue_cannot_be_filed_against_another_workspaces_team(world):
    """`teamId` is the client's now, which makes this the new way in.

    A member of A naming A's slug and B's team is authorized for the
    workspace and refused by `issues_team_fk` against it, inside the insert.
    Nothing lands in either workspace, and B's counter does not move -- a
    number allocated against B's team would be a cross-tenant write even if
    no issue row survived it.
    """
    context, connection, _ = world

    before = await connection.fetchval(
        "SELECT issue_counter FROM teams WHERE id = $1", TEAM_B
    )

    result = await run(
        context,
        "mutation M($slug: String!, $team: UUID!) "
        '{ issueCreate(input: {workspaceSlug: $slug, teamId: $team, title: "misfiled"}) '
        "{ issue { id } errors { field code } } }",
        slug=SLUG_A,
        team=str(TEAM_B),
    )

    # A team that is not this workspace's is not a rule the client can fix by
    # editing a field, so it is not translated into a payload error: it
    # propagates and is masked.
    assert result.data is None or result.data["issueCreate"] is None

    misfiled = await connection.fetchval(
        "SELECT count(*) FROM issues WHERE title = $1", "misfiled"
    )

    assert misfiled == 0
    assert (
        await connection.fetchval(
            "SELECT issue_counter FROM teams WHERE id = $1", TEAM_B
        )
        == before
    )


async def test_a_created_issue_lands_in_the_named_workspace_and_team(world):
    """The positive half of the write path, end to end through the schema.

    Inherited from the suite that used to cover the default-team seam: with
    no default left, "the row lands where the client said" is a claim about
    the client's `teamId` and the viewer's authorized workspace, and it is
    still the claim that matters most. The row is read back over the two
    columns the API never returns -- an entity cannot witness its own tenant.

    The follow-up read is part of the same test on purpose: the write path
    and the read path authorize independently, so an issue written into one
    workspace and listed from another would leave both halves passing their
    own tests.
    """
    context, connection, _ = world

    created = await run(
        context,
        "mutation M($slug: String!, $team: UUID!) "
        '{ issueCreate(input: {workspaceSlug: $slug, teamId: $team, title: "filed"}) '
        "{ issue { id identifier } errors { field code } } }",
        slug=SLUG_A,
        team=str(TEAM_A),
    )

    assert created.errors is None
    assert created.data["issueCreate"]["errors"] == []

    issue = created.data["issueCreate"]["issue"]

    assert issue["identifier"].startswith("ALPHA-")

    row = await connection.fetchrow(
        "SELECT workspace_id, team_id, creator_id FROM issues WHERE id = $1",
        UUID(issue["id"]),
    )

    assert row["workspace_id"] == WORKSPACE_A
    assert row["team_id"] == TEAM_A

    # Authorship comes from the membership that authorized the call, not from
    # anything the document said -- there is no input field for it.
    assert row["creator_id"] == USER_A

    listed = await run(context, SCOPED_QUERIES["issues"], slug=SLUG_A)

    assert listed.errors is None
    assert issue["id"] in {node["id"] for node in listed.data["issues"]["nodes"]}
