"""The rest of the read surface, under the discipline `issue` already has.

`tests/test_graphql_workspace_scope_db.py` proves the property for issues,
labels, cycles, projects, teams, comments and relations. Six entities have
been added to the schema since and none of them is in that file's tables:
documents, initiatives, saved views, releases, label groups and issue
templates -- plus the three fields that take a foreign id as a NARROWING
rather than as a subject (`issueSubscribers`, `triageIssues`,
`issueTemplates(teamId:)`).

The property is one sentence, and it is stronger than "the row is not
returned":

    an id belonging to another workspace must be INDISTINGUISHABLE from an
    id that exists nowhere -- the same bytes, the same errors, the same
    absence of them.

That is the half no membership check can supply. A caller who names their
OWN slug is unquestionably authorized for it, so `authorized_scope` says yes
and the only thing left between them and another tenant's row is the
`workspace_id` equality in the WHERE clause. If a lookup answered "not
found" for a uuid nobody holds and "found nothing, but here is an error"
for one that is real elsewhere, the difference is a membership oracle for
every id an attacker can guess.

So every read below is run three times -- foreign id, absent id, and (for
the writes) a re-read of the victim row afterwards -- and the first two
results are compared as FORMATTED dictionaries rather than as "both are
falsy". `frontend/e2e/journeys.spec.ts` makes the same comparison for
`issue` over the wire, on raw response text; this is that assertion moved
in far enough to cover fields the browser suite does not visit.

The last section is the tombstone. Since migration 026 a removed member
keeps their row in `workspace_members`, so `find_membership`'s
`removed_at IS NULL` is the only thing standing between "left the company"
and "still has every permission they had". `tests/test_members_invites_db.py`
asserts the service refuses them; this asserts the API answers them exactly
as it answers a stranger, which is the part a client can observe.

Marked `db`: deselected by default, skipped when Docker is unreachable.
Nothing here touches DATABASE_URL or Neon.
"""

from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.domain.issues import IssueFilter, IssueOrder
from app.domain.templates import IssueTemplateDraft
from app.domain.tenancy import AuthorizedWorkspaceScope, WorkspaceScope
from app.graphql.schema import build_schema
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE
from app.repositories.activity import ActivityRepository
from app.repositories.documents import DocumentRepository
from app.repositories.initiatives import InitiativeRepository
from app.repositories.invitations import InvitationRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.issues import IssueRepository
from app.repositories.label_groups import LabelGroupRepository
from app.repositories.labels import LabelRepository
from app.repositories.memberships import MembershipRepository
from app.repositories.notifications import NotificationRepository
from app.repositories.recurrences import RecurrenceRepository
from app.repositories.relations import RelationRepository
from app.repositories.releases import ReleaseRepository
from app.repositories.saved_views import FavoriteRepository, SavedViewRepository
from app.repositories.subscribers import SubscriberRepository
from app.repositories.teams import TeamRepository
from app.repositories.templates import TemplateRepository
from app.repositories.triage import TriageRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.activity import ActivityService
from app.services.documents import DocumentService
from app.services.initiatives import InitiativeService
from app.services.issues import IssueService
from app.services.labels import LabelService
from app.services.memberships import MembershipService
from app.services.releases import ReleaseService
from app.services.saved_views import FavoriteService, SavedViewService
from app.services.teams import TeamService
from app.services.templates import TemplateService
from app.services.triage import TriageService

from tests.conftest import (
    apply_all_migrations,
    graphql_context,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

schema = build_schema("test")

WORKSPACE_A = UUID("00000000-0000-7000-8000-0000000000c0")
TEAM_A = UUID("00000000-0000-7000-8000-0000000000c1")
SLUG_A = "carbon"
USER_A = UUID("00000000-0000-7000-8000-0000000000c9")

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000d0")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000d1")
SLUG_B = "dalton"
USER_B = UUID("00000000-0000-7000-8000-0000000000d9")

# A member of A who is removed partway through the last section. Separate from
# USER_A so that the removal does not empty the workspace of the account every
# other test authenticates as.
LEAVER = UUID("00000000-0000-7000-8000-0000000000ca")

# Never a member of anything. The control the former member is measured
# against: "refused" is only the right answer if it is the SAME refusal a
# complete stranger gets.
STRANGER = UUID("00000000-0000-7000-8000-0000000000cb")

# One GitHub repository per workspace, because `releases_repository_fk` is
# composite against (workspace_id, repository_id) and a release cannot be
# written without one. The ids differ per workspace so that a release in A
# cannot accidentally satisfy B's foreign key.
REPOSITORY_A = 9001
REPOSITORY_B = 9002

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40

INSERT_WORKSPACE = "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)"
INSERT_TEAM = "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)"
INSERT_USER = """
INSERT INTO users (id, email, password_hash)
VALUES ($1, $2, '$argon2id$not-a-real-hash')
"""
INSERT_MEMBERSHIP = """
INSERT INTO workspace_members (workspace_id, user_id, role)
VALUES ($1, $2, $3)
"""
INSERT_INSTALLATION = """
INSERT INTO github_installations (workspace_id, installation_id, connected_by)
VALUES ($1, $2, $3)
"""
INSERT_REPOSITORY = """
INSERT INTO github_repositories (workspace_id, repository_id, full_name)
VALUES ($1, $2, $3)
"""


def _services(pool):
    """Every service the fields below reach, over one pool.

    Mirrors `app.graphql.context.get_context`'s wiring rather than inventing
    its own, because a service composed differently here would be testing a
    composition no request ever gets. What is left out is only what no field
    in this file resolves.
    """
    teams = TeamService(pool=pool, repository=TeamRepository())
    issues = IssueService(pool=pool, repository=IssueRepository(), teams=teams)
    labels = LabelService(
        pool=pool,
        repository=LabelRepository(),
        issue_label_repository=IssueLabelRepository(),
        group_repository=LabelGroupRepository(),
    )

    return SimpleNamespace(
        team_service=teams,
        issue_service=issues,
        label_service=labels,
        document_service=DocumentService(pool=pool, repository=DocumentRepository()),
        initiative_service=InitiativeService(
            pool=pool, repository=InitiativeRepository()
        ),
        release_service=ReleaseService(pool=pool, repository=ReleaseRepository()),
        saved_view_service=SavedViewService(
            pool=pool,
            repository=SavedViewRepository(),
            favorites=FavoriteRepository(),
        ),
        favorite_service=FavoriteService(pool=pool, repository=FavoriteRepository()),
        template_service=TemplateService(
            pool=pool,
            repository=TemplateRepository(),
            recurrences=RecurrenceRepository(),
            issues=issues,
            labels=labels,
        ),
        triage_service=TriageService(
            pool=pool,
            repository=TriageRepository(),
            relations=RelationRepository(),
            teams=teams,
        ),
        activity_service=ActivityService(
            pool=pool,
            repository=ActivityRepository(),
            notifications=NotificationRepository(),
            subscribers=SubscriberRepository(),
        ),
        membership_service=MembershipService(
            pool=pool,
            repository=MembershipRepository(),
            workspaces=WorkspaceRepository(),
            invitations=InvitationRepository(),
        ),
    )


async def _seed_tenant(
    services, *, workspace_id, team_id, user_id, repository_id, commit_sha, key
):
    """One of everything, through the services the product writes with.

    Seeding by hand would write rows the API cannot produce, and a tenancy
    test over rows the API cannot produce proves nothing about the API.
    """
    scope = WorkspaceScope(workspace_id=workspace_id)
    authorized = AuthorizedWorkspaceScope(
        workspace_id=workspace_id,
        user_id=user_id,
        role="owner",
    )

    issue = await services.issue_service.create(
        scope=scope,
        team_id=team_id,
        title=f"{key} issue",
        description=None,
        priority=0,
        creator_id=user_id,
    )
    document = await services.document_service.create(
        scope=scope,
        title=f"{key} document",
        content={"type": "doc", "content": []},
        project_id=None,
        initiative_id=None,
        creator_id=user_id,
    )
    initiative = await services.initiative_service.create(
        scope=scope,
        name=f"{key} initiative",
        description=None,
        status="planned",
        target_date=date(2026, 9, 1),
        owner_id=user_id,
    )
    group = await services.label_service.create_group(
        scope=scope,
        name=f"{key} group",
        exclusive=False,
    )
    template = await services.template_service.create(
        scope=scope,
        draft=IssueTemplateDraft(name=f"{key} template", team_id=team_id),
    )
    saved_view = await services.saved_view_service.create(
        scope=authorized,
        name=f"{key} view",
        team_id=team_id,
        issue_filter=IssueFilter(),
        order=IssueOrder(),
        layout="list",
        grouping=None,
        subgrouping=None,
        visibility="shared",
    )
    environment = await services.release_service.create_environment(
        scope=scope,
        name=f"{key} production",
        kind="production",
    )
    release = await services.release_service.create(
        scope=scope,
        name=f"{key} 1.0.0",
        environment_id=environment.id,
        repository_id=repository_id,
        commit_sha=commit_sha,
    )
    await services.activity_service.subscribe(scope=authorized, issue_id=issue.id)

    return SimpleNamespace(
        issue=issue.id,
        document=document.id,
        initiative=initiative.id,
        labelGroup=group.id,
        issueTemplate=template.id,
        savedView=saved_view.id,
        release=release.id,
        environment=environment.id,
        team=team_id,
    )


@pytest.fixture
async def world(postgres_dsn):
    """Two tenants with one of every entity each, and a real membership table.

    Yields (context, connection, seeded). The context's viewer is USER_A --
    an owner of A and a stranger to B -- and a test that wants somebody else
    replaces `context.viewer`.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)

        for workspace_id, slug, name, team_id, key, user_id, repo in (
            (WORKSPACE_A, SLUG_A, "Carbon", TEAM_A, "CAR", USER_A, REPOSITORY_A),
            (WORKSPACE_B, SLUG_B, "Dalton", TEAM_B, "DAL", USER_B, REPOSITORY_B),
        ):
            await connection.execute(INSERT_WORKSPACE, workspace_id, slug, name)
            await connection.execute(INSERT_TEAM, team_id, workspace_id, name, key)
            await seed_workflow_states(connection, workspace_id, team_id)
            await connection.execute(INSERT_USER, user_id, f"{slug}@example.test")
            await connection.execute(INSERT_MEMBERSHIP, workspace_id, user_id, "owner")
            await connection.execute(
                INSERT_INSTALLATION, workspace_id, repo * 10, user_id
            )
            await connection.execute(
                INSERT_REPOSITORY, workspace_id, repo, f"acme/{slug}"
            )

        # Two more accounts in A: one who will be removed, and one who never
        # joins anything. Both exist as users so that the last section can
        # authenticate as either.
        for user_id, email in (
            (LEAVER, "leaver@example.test"),
            (STRANGER, "stranger@example.test"),
        ):
            await connection.execute(INSERT_USER, user_id, email)

        await connection.execute(INSERT_MEMBERSHIP, WORKSPACE_A, LEAVER, "member")

        pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=4)

        try:
            services = _services(pool)

            seeded = {
                "a": await _seed_tenant(
                    services,
                    workspace_id=WORKSPACE_A,
                    team_id=TEAM_A,
                    user_id=USER_A,
                    repository_id=REPOSITORY_A,
                    commit_sha=COMMIT_A,
                    key="a",
                ),
                "b": await _seed_tenant(
                    services,
                    workspace_id=WORKSPACE_B,
                    team_id=TEAM_B,
                    user_id=USER_B,
                    repository_id=REPOSITORY_B,
                    commit_sha=COMMIT_B,
                    key="b",
                ),
            }

            context = graphql_context(**vars(services))

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


def formatted(result):
    """A result as the client would see it: data, plus every error verbatim.

    Comparing this rather than `result.data` alone is the whole point. A
    lookup that returned null for both cases but attached an error to one of
    them would pass a data-only assertion and still be an oracle.
    """
    return {
        "data": result.data,
        "errors": [error.formatted for error in result.errors or []],
    }


# --------------------------------------------------------------------------
# One row, by id, under the viewer's own slug
# --------------------------------------------------------------------------


# field -> document. Every one of these is nullable and takes the same two
# arguments, which is what lets one table drive both the foreign-id and the
# absent-id runs. Written out rather than derived from the schema, because
# the list IS the claim: a by-id field added without an entry here is a field
# nothing in this repository covers, and a reviewer diffing this against
# `type Query` is what catches it.
BY_ID_QUERIES = {
    "document": (
        "query Q($slug: String!, $id: UUID!) "
        "{ document(workspaceSlug: $slug, id: $id) { id title } }"
    ),
    "initiative": (
        "query Q($slug: String!, $id: UUID!) "
        "{ initiative(workspaceSlug: $slug, id: $id) { id name } }"
    ),
    "labelGroup": (
        "query Q($slug: String!, $id: UUID!) "
        "{ labelGroup(workspaceSlug: $slug, id: $id) { id name } }"
    ),
    "issueTemplate": (
        "query Q($slug: String!, $id: UUID!) "
        "{ issueTemplate(workspaceSlug: $slug, id: $id) { id name } }"
    ),
    "savedView": (
        "query Q($slug: String!, $id: UUID!) "
        "{ savedView(workspaceSlug: $slug, id: $id) { id name } }"
    ),
    "release": (
        "query Q($slug: String!, $id: UUID!) "
        "{ release(workspaceSlug: $slug, id: $id) { id name } }"
    ),
}


@pytest.mark.parametrize("field", sorted(BY_ID_QUERIES))
async def test_another_workspaces_id_is_the_same_answer_as_no_id_at_all(field, world):
    """The headline, and the only assertion in this file that matters most.

    A's owner names A's slug -- which they are unquestionably authorized for,
    so nothing about membership can refuse this -- and B's id. The answer has
    to be null, with no error, and byte-for-byte what a uuid nobody holds
    gets. Anything else turns a guessed id into a membership test for another
    tenant.
    """
    context, _, seeded = world

    foreign = await run(
        context,
        BY_ID_QUERIES[field],
        slug=SLUG_A,
        id=str(getattr(seeded["b"], field)),
    )
    absent = await run(
        context,
        BY_ID_QUERIES[field],
        slug=SLUG_A,
        id=str(uuid4()),
    )

    assert formatted(foreign) == {"data": {field: None}, "errors": []}
    assert formatted(foreign) == formatted(absent)


@pytest.mark.parametrize("field", sorted(BY_ID_QUERIES))
async def test_the_viewers_own_id_still_resolves(field, world):
    """The control, without which every assertion above passes vacuously.

    A schema that answered null for everything would satisfy the whole file
    except here.
    """
    context, _, seeded = world

    result = await run(
        context,
        BY_ID_QUERIES[field],
        slug=SLUG_A,
        id=str(getattr(seeded["a"], field)),
    )

    assert result.errors is None
    assert result.data[field]["id"] == str(getattr(seeded["a"], field))


async def test_naming_the_other_workspace_is_not_found(world):
    """And the slug route, which membership DOES refuse.

    Both halves are needed. This one proves the scope check runs; the one
    above proves the WHERE clause does, which is the half that survives a
    caller who is authorized for the slug they named.

    One test over all six documents rather than six parametrised ones,
    because the `world` fixture rebuilds the whole schema per test and the
    check here is the same shared helper for every field -- unlike the
    per-entity WHERE clauses above, which really are six separate risks.
    """
    context, _, seeded = world

    for field, document in sorted(BY_ID_QUERIES.items()):
        result = await run(
            context,
            document,
            slug=SLUG_B,
            id=str(getattr(seeded["b"], field)),
        )

        assert result.data == {field: None}, field
        assert result.errors is not None and len(result.errors) == 1, field

        reported = result.errors[0].formatted

        assert reported["message"] == WORKSPACE_NOT_FOUND_MESSAGE, field
        assert reported["extensions"] == {"code": "NOT_FOUND"}, field


# --------------------------------------------------------------------------
# Foreign ids used as a NARROWING rather than as a subject
# --------------------------------------------------------------------------


# These are the shape `cycles(teamId:)` is, and the one most likely to be got
# wrong: the id is not what the field returns, it is what the field filters
# by, so a missing workspace predicate does not show up as a leaked row --
# it shows up as somebody else's rows in your own list.
NARROWING_QUERIES = {
    "issueSubscribers": (
        "query Q($slug: String!, $id: UUID!) "
        "{ issueSubscribers(workspaceSlug: $slug, issueId: $id) { userId } }",
        "issue",
    ),
    "issueViewerIsSubscribed": (
        "query Q($slug: String!, $id: UUID!) "
        "{ issueViewerIsSubscribed(workspaceSlug: $slug, issueId: $id) }",
        "issue",
    ),
    "triageIssues": (
        "query Q($slug: String!, $id: UUID!) "
        "{ triageIssues(workspaceSlug: $slug, teamId: $id) "
        "{ nodes { issue { id } } } }",
        "team",
    ),
    "triageCount": (
        "query Q($slug: String!, $id: UUID!) "
        "{ triageCount(workspaceSlug: $slug, teamId: $id) }",
        "team",
    ),
    "issueTemplates": (
        "query Q($slug: String!, $id: UUID!) "
        "{ issueTemplates(workspaceSlug: $slug, teamId: $id) { id } }",
        "team",
    ),
    "savedViews": (
        "query Q($slug: String!, $id: UUID!) "
        "{ savedViews(workspaceSlug: $slug, teamId: $id) { nodes { id } } }",
        "team",
    ),
    "documents": (
        "query Q($slug: String!, $id: UUID!) "
        "{ documents(workspaceSlug: $slug, initiativeId: $id) { nodes { id } } }",
        "initiative",
    ),
}


@pytest.mark.parametrize("field", sorted(NARROWING_QUERIES))
async def test_a_foreign_id_narrows_to_nothing_rather_than_widening(field, world):
    """A filter naming another tenant's row selects nothing, and says so the
    same way an id that exists nowhere does.

    The failure this guards against is not a leaked row: it is a statement
    that filters on `team_id = $2` and forgets `workspace_id = $1`, which
    answers a foreign team id with THIS workspace's rows or with the other
    workspace's, depending on which side of the join the predicate landed.
    Both are visible here as a difference from the absent-id run.
    """
    context, _, seeded = world
    document, attribute = NARROWING_QUERIES[field]

    foreign = await run(
        context,
        document,
        slug=SLUG_A,
        id=str(getattr(seeded["b"], attribute)),
    )
    absent = await run(context, document, slug=SLUG_A, id=str(uuid4()))

    assert foreign.errors is None, [e.formatted for e in foreign.errors or []]
    assert formatted(foreign) == formatted(absent)


async def test_the_subscriber_list_is_not_empty_for_the_viewers_own_issue(world):
    """The control for the two subscriber fields above.

    `_seed_tenant` subscribes the owner to their own issue, so A's issue has
    a watcher. Without this, "the foreign id answered an empty list" would be
    satisfied by a field that answers an empty list for everything.
    """
    context, _, seeded = world
    document, _ = NARROWING_QUERIES["issueSubscribers"]

    result = await run(context, document, slug=SLUG_A, id=str(seeded["a"].issue))

    assert result.errors is None
    assert [row["userId"] for row in result.data["issueSubscribers"]] == [str(USER_A)]


# --------------------------------------------------------------------------
# Writes: the same id, under the viewer's own slug
# --------------------------------------------------------------------------


# field -> (mutation, table). Every delete for the six entities above takes
# exactly `{workspaceSlug, id}`, so one document shape covers all of them,
# and every payload carries `errors` under a differently-named id -- which is
# why only `errors` is selected.
#
# The table is here because the row has to be read back over the RAW
# connection. Reading it through the API would refuse the id for the very
# reason under test, and would therefore report "still there" whether or not
# it was.
DELETE_MUTATIONS = {
    "document": ("documentDelete", "documents"),
    "initiative": ("initiativeDelete", "initiatives"),
    "labelGroup": ("labelGroupDelete", "label_groups"),
    "issueTemplate": ("issueTemplateDelete", "issue_templates"),
    "savedView": ("savedViewDelete", "saved_views"),
    "release": ("releaseDelete", "releases"),
}


async def _count(connection, table, workspace_id, row_id):
    # The table name is interpolated and the two values are bound. It comes
    # from the literal dict above and never from anything a request carries,
    # which is the only form of interpolation this repository permits; see
    # `MembershipRepository`'s note on why no SQL in `app/` is assembled at
    # all.
    return await connection.fetchval(
        f"SELECT count(*) FROM {table} "  # noqa: S608
        "WHERE workspace_id = $1 AND id = $2",
        workspace_id,
        row_id,
    )


@pytest.mark.parametrize("field", sorted(DELETE_MUTATIONS))
async def test_a_delete_reaches_the_viewers_own_row_and_not_the_other_tenants(
    field, world
):
    """The write half, and the one where a missing predicate is destructive.

    A read that forgot `workspace_id` discloses a row. A DELETE that forgot it
    REMOVES one, in a tenant the caller cannot even see -- so the assertion
    that matters is not the mutation's own answer (a delete that succeeded
    against another tenant's row also reports no errors) but the victim row
    still being there afterwards.

    Both halves in one test on purpose. The claim is a difference: the same
    document, against the same kind of row, under the same slug and the same
    viewer, must delete one and not the other -- and a test that only asserted
    the refusal would be satisfied by a mutation that refuses everything.
    """
    context, connection, seeded = world
    mutation, table = DELETE_MUTATIONS[field]
    document = (
        "mutation M($slug: String!, $id: UUID!) { "
        f"{mutation}(input: {{workspaceSlug: $slug, id: $id}}) "
        "{ errors { field code message } } }"
    )

    victim = getattr(seeded["b"], field)

    await run(context, document, slug=SLUG_A, id=str(victim))

    assert await _count(connection, table, WORKSPACE_B, victim) == 1

    own = getattr(seeded["a"], field)
    accepted = await run(context, document, slug=SLUG_A, id=str(own))

    assert accepted.errors is None, [e.formatted for e in accepted.errors or []]
    assert accepted.data[mutation]["errors"] == []
    assert await _count(connection, table, WORKSPACE_A, own) == 0


# --------------------------------------------------------------------------
# The tombstone: a former member, and a stranger, get the same answer
# --------------------------------------------------------------------------


async def _as(context, user_id):
    async def viewer():
        return SimpleNamespace(id=user_id)

    context.viewer = viewer


async def test_a_former_member_is_refused_exactly_as_a_stranger_is(world):
    """026's `removed_at` is a permission boundary, and this is where it shows.

    A removed member's row survives -- that is the whole point of the
    tombstone, because seven foreign keys onto `workspace_members` are
    authorship and a comment cannot be reassigned to nobody. The predicate
    `removed_at IS NULL` in `find_membership` is therefore the ONLY thing
    that stops that surviving row from continuing to authorize them, and a
    predicate is one refactor away from being dropped as redundant.

    Asserted against a stranger rather than against a literal, so the claim
    is an equivalence and not a message: a former member must not be
    distinguishable from somebody who was never here. A distinguishable
    refusal would tell whoever holds the credentials of a departed account
    that the account was once real in this workspace.
    """
    context, _, seeded = world

    await _as(context, LEAVER)

    before = {
        field: await run(
            context, document, slug=SLUG_A, id=str(getattr(seeded["a"], field))
        )
        for field, document in BY_ID_QUERIES.items()
    }

    # Still a member at this point, so every row resolves. Without this the
    # assertions below would pass for a `LEAVER` who never had access at all.
    for field, result in before.items():
        assert result.data[field] is not None, field

    await context.membership_service.remove_member(
        scope=AuthorizedWorkspaceScope(
            workspace_id=WORKSPACE_A,
            user_id=USER_A,
            role="owner",
        ),
        user_id=LEAVER,
    )

    after = {
        field: await run(
            context, document, slug=SLUG_A, id=str(getattr(seeded["a"], field))
        )
        for field, document in BY_ID_QUERIES.items()
    }

    await _as(context, STRANGER)

    stranger = {
        field: await run(
            context, document, slug=SLUG_A, id=str(getattr(seeded["a"], field))
        )
        for field, document in BY_ID_QUERIES.items()
    }

    for field in BY_ID_QUERIES:
        assert formatted(after[field]) == formatted(stranger[field]), field
        assert after[field].data == {field: None}, field
        assert after[field].errors[0].formatted["extensions"] == {
            "code": "NOT_FOUND"
        }, field


async def test_a_former_member_loses_the_workspace_switcher_too(world):
    """The list a client builds its navigation from.

    Refusing every scoped field while still listing the workspace would leave
    a former employee looking at a sidebar full of tenants that reject them on
    arrival -- and would leak that they were once in each one.
    """
    context, _, _ = world

    await _as(context, LEAVER)

    document = "{ myWorkspaces { workspace { slug } } }"

    before = await run(context, document)

    assert [row["workspace"]["slug"] for row in before.data["myWorkspaces"]] == [SLUG_A]

    await context.membership_service.remove_member(
        scope=AuthorizedWorkspaceScope(
            workspace_id=WORKSPACE_A,
            user_id=USER_A,
            role="owner",
        ),
        user_id=LEAVER,
    )

    after = await run(context, document)

    await _as(context, STRANGER)

    stranger = await run(context, document)

    assert formatted(after) == formatted(stranger)
    assert after.data == {"myWorkspaces": []}
