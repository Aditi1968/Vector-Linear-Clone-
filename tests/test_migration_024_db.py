"""Migration 024 applied by the real runner, then asked what it built -- and
the release feature run end to end against it.

A release is a *reading* of migration 017's tables over a commit range, frozen
into a document. Two properties have to hold and neither can be proved without
a server:

  * The reading is correct. "The issues touched between these two commits" is a
    window over `github_commits.committed_at` UNIONed with a window over
    `github_pull_requests.merged_at`, and the only way to know it selects the
    right rows -- and excludes the ones just outside -- is to seed a repository
    whose history straddles the boundary and ask.
  * The reading cannot cross a tenant. Every id a release names arrives from a
    browser: an environment, a repository, a commit SHA, and -- through the
    range -- a set of issues resolved from identifiers that 017's header
    explains are written by whoever opened a pull request. A release note gets
    pasted into changelogs and sent to customers, so a link that reached
    `release_issues` from a hostile pull-request title would publish another
    tenant's issue title. The refusals below are what makes that unstorable
    rather than merely unlikely.

And the third property, which is the point of the whole design: the SAME range
renders the SAME bytes. tests/test_releases.py proves that of the pure
function; this proves it of the function driven by the real repository over the
real schema, which is where an ORDER BY could quietly become the only reason it
was true.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import ValidationError
from app.domain.patch import UNSET
from app.domain.releases import (
    ENVIRONMENT_KINDS,
    RANGE_LIMIT,
    RELEASE_STATUSES,
    TRUNCATED,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.releases import ReleaseRepository
from app.services.releases import ReleaseService

from tests.conftest import (
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# The other tenant: a real workspace with a real repository, a real environment
# and a real issue, whose rows every cross-tenant assertion below tries and
# fails to reach. It has to be real -- a nonexistent id would be refused by any
# spelling of these constraints and would prove nothing about which one is in
# force.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

BOOTSTRAP_ENVIRONMENT_ID = UUID("00000000-0000-7000-8000-0000000000d1")
OTHER_ENVIRONMENT_ID = UUID("00000000-0000-7000-8000-0000000000d2")

BOOTSTRAP_REPOSITORY_ID = 11111
OTHER_REPOSITORY_ID = 22222

BOOTSTRAP_SCOPE = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

# The history the range assertions are about, laid out on a line so that
# "before", "inside" and "after" are readable rather than arithmetic.
#
#   T0        base commit, and PR #7's merge          <- OUTSIDE (before)
#   T1        commit c1                               <- inside
#   T2        commit c2, and PR #84's merge           <- inside
#   T3        head commit                             <- inside (the upper end)
#   T4        commit c4                               <- OUTSIDE (after)
#
# The release under test spans (T0, T3], so exactly one row sits on each side
# of each boundary. A window off by one in either direction changes the answer,
# which a fixture where everything was comfortably inside would not.
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)
T2 = T0 + timedelta(days=2)
T3 = T0 + timedelta(days=3)
T4 = T0 + timedelta(days=4)


def sha(prefix: str) -> str:
    """A well-formed 40-character SHA, readable at its front.

    `github_commits_sha_format` and `releases_commit_sha_format` both demand 40
    LOWERCASE HEXADECIMAL characters, so every prefix below is spelled out of
    that alphabet -- `head` is not a SHA and the constraint says so, which is
    how this helper was written wrong the first time.
    """
    return (prefix + "0" * 40)[:40]


BASE_SHA = sha("ba5e")
C1_SHA = sha("c1")
C2_SHA = sha("c2")
HEAD_SHA = sha("deadbeef")
C4_SHA = sha("c4")

OTHER_SHA = sha("07be5")

# Issues in the bootstrap workspace, by the number they carry.
BEFORE_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b0")
C1_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
C2_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b2")
HEAD_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b3")
PULL_ONLY_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b4")
AFTER_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b5")

OTHER_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000bf")

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

# Confirmed on the way in. `github_repositories` hangs off the installation,
# and 016 refuses an account login on a claim nobody confirmed, so the honest
# way to get a usable installation here is to date the confirmation rather than
# to leave the row half-built.
INSERT_INSTALLATION_SQL = """
INSERT INTO github_installations
    (workspace_id, installation_id, connected_by, confirmed_at)
VALUES ($1, $2, $3, now())
"""

INSERT_REPOSITORY_SQL = """
INSERT INTO github_repositories (workspace_id, repository_id, full_name)
VALUES ($1, $2, $3)
"""

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, number, workflow_state_id, title, priority
)
VALUES (
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
    ),
    $5, 1
)
"""

INSERT_COMMIT_SQL = """
INSERT INTO github_commits
    (workspace_id, repository_id, sha, message, committed_at)
VALUES ($1, $2, $3, $4, $5)
"""

INSERT_COMMIT_LINK_SQL = """
INSERT INTO github_commit_issues (workspace_id, repository_id, sha, issue_id)
VALUES ($1, $2, $3, $4)
"""

INSERT_PULL_REQUEST_SQL = """
INSERT INTO github_pull_requests
    (workspace_id, repository_id, number, title, state, merged_at)
VALUES ($1, $2, $3, $4, 'closed', $5)
"""

INSERT_PULL_LINK_SQL = """
INSERT INTO github_pull_request_issues
    (workspace_id, repository_id, number, issue_id, source)
VALUES ($1, $2, $3, $4, 'title')
"""

INSERT_ENVIRONMENT_SQL = """
INSERT INTO environments (id, workspace_id, name, kind)
VALUES ($1, $2, $3, $4)
"""

INSERT_RELEASE_SQL = """
INSERT INTO releases (
    workspace_id, environment_id, repository_id, name,
    commit_sha, previous_commit_sha, status, notes, deployed_at
)
VALUES ($1, $2, $3, $4, $5, $6, $7, 'notes', $8)
RETURNING id
"""

INSERT_RELEASE_ISSUE_SQL = """
INSERT INTO release_issues (workspace_id, release_id, issue_id)
VALUES ($1, $2, $3)
"""

# The values a CHECK admits, read back out of the catalog rather than out of
# the file. `pg_get_constraintdef` renders what the server actually enforces,
# which is the thing a drifting constant would disagree with.
CONSTRAINT_DEFINITION_SQL = """
SELECT pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conname = $1
"""


@pytest.fixture
async def connection(postgres_dsn):
    """A migrated database with two fully-populated tenants."""
    conn = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(conn)
        await apply_all_migrations(conn)
        await seed(conn)

        yield conn
    finally:
        await reset_schema(conn)
        await conn.close()


@pytest.fixture
async def releases(postgres_dsn, connection):
    """A real `ReleaseService` over the seeded database.

    Its own pool alongside the fixture's connection: services acquire, and the
    seeded connection is what the assertions read back through -- the shape
    tests/test_migration_020_db.py uses for the same reason.
    """
    pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=4)

    try:
        yield ReleaseService(pool=pool, repository=ReleaseRepository())
    finally:
        await pool.close()


async def seed(connection) -> None:
    """Two workspaces, each with a team, a member, a repository, an environment
    and issues; and one repository with a history that straddles the range.

    Symmetric on purpose. Every cross-tenant assertion below is "workspace A's
    row pointing at workspace B's row", and a lopsided fixture -- where one side
    lacks the thing the other is reaching for -- would pass those assertions for
    the wrong reason.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        OTHER_WORKSPACE_ID,
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) "
        "VALUES ($1, $2, 'Other', 'OTH')",
        OTHER_TEAM_ID,
        OTHER_WORKSPACE_ID,
    )

    # 005 seeds workflow states for the teams that exist when it runs, so the
    # team created above -- afterwards -- has none, and an issue on it cannot
    # be inserted at all.
    await seed_workflow_states(connection, OTHER_WORKSPACE_ID, OTHER_TEAM_ID)

    for user_id in (MEMBER_ID, OUTSIDER_ID):
        await connection.execute(INSERT_USER_SQL, user_id)

    for workspace_id, user_id, installation_id in (
        (BOOTSTRAP_WORKSPACE_ID, MEMBER_ID, 4242),
        (OTHER_WORKSPACE_ID, OUTSIDER_ID, 5353),
    ):
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, 'admin')",
            workspace_id,
            user_id,
        )
        await connection.execute(
            INSERT_INSTALLATION_SQL, workspace_id, installation_id, user_id
        )

    for workspace_id, repository_id, full_name in (
        (BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_REPOSITORY_ID, "acme/vector"),
        (OTHER_WORKSPACE_ID, OTHER_REPOSITORY_ID, "other/secret"),
    ):
        await connection.execute(
            INSERT_REPOSITORY_SQL, workspace_id, repository_id, full_name
        )

    for environment_id, workspace_id in (
        (BOOTSTRAP_ENVIRONMENT_ID, BOOTSTRAP_WORKSPACE_ID),
        (OTHER_ENVIRONMENT_ID, OTHER_WORKSPACE_ID),
    ):
        await connection.execute(
            INSERT_ENVIRONMENT_SQL,
            environment_id,
            workspace_id,
            "Production",
            "production",
        )

    for issue_id, number, title in (
        (BEFORE_ISSUE_ID, 1, "Shipped before the range"),
        (C1_ISSUE_ID, 2, "Landed on day one"),
        (C2_ISSUE_ID, 3, "Landed on day two"),
        (HEAD_ISSUE_ID, 4, "Landed at the head"),
        (PULL_ONLY_ISSUE_ID, 5, "Linked only by a pull request title"),
        (AFTER_ISSUE_ID, 6, "Landed after the range"),
    ):
        await connection.execute(
            INSERT_ISSUE_SQL,
            issue_id,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
            number,
            title,
        )

    await connection.execute(
        INSERT_ISSUE_SQL,
        OTHER_ISSUE_ID,
        OTHER_WORKSPACE_ID,
        OTHER_TEAM_ID,
        1,
        "Theirs",
    )

    for commit_sha, committed_at, issue_id in (
        (BASE_SHA, T0, BEFORE_ISSUE_ID),
        (C1_SHA, T1, C1_ISSUE_ID),
        (C2_SHA, T2, C2_ISSUE_ID),
        (HEAD_SHA, T3, HEAD_ISSUE_ID),
        (C4_SHA, T4, AFTER_ISSUE_ID),
    ):
        await connection.execute(
            INSERT_COMMIT_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            commit_sha,
            f"Work on {commit_sha[:7]}",
            committed_at,
        )
        await connection.execute(
            INSERT_COMMIT_LINK_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            commit_sha,
            issue_id,
        )

    # Two merged pull requests, one on each side of the lower boundary. #84 is
    # the case the UNION exists for: its issue is named by the TITLE and by no
    # commit message at all, which is what a squash merge produces.
    for number, merged_at, issue_id, title in (
        (7, T0, BEFORE_ISSUE_ID, "CORE-1 Older work"),
        (84, T2, PULL_ONLY_ISSUE_ID, "CORE-5 Fix the OAuth callback"),
    ):
        await connection.execute(
            INSERT_PULL_REQUEST_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            number,
            title,
            merged_at,
        )
        await connection.execute(
            INSERT_PULL_LINK_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            number,
            issue_id,
        )

    # The other tenant's own commit, so "not found" below means "not in your
    # workspace" rather than "nowhere in this database".
    await connection.execute(
        INSERT_COMMIT_SQL,
        OTHER_WORKSPACE_ID,
        OTHER_REPOSITORY_ID,
        OTHER_SHA,
        "Their work",
        T2,
    )
    await connection.execute(
        INSERT_COMMIT_LINK_SQL,
        OTHER_WORKSPACE_ID,
        OTHER_REPOSITORY_ID,
        OTHER_SHA,
        OTHER_ISSUE_ID,
    )


async def insert_bootstrap_release(
    connection,
    *,
    name: str = "v1.4.0",
    status: str = "pending",
    deployed_at=None,
    environment_id: UUID = BOOTSTRAP_ENVIRONMENT_ID,
    repository_id: int = BOOTSTRAP_REPOSITORY_ID,
    commit_sha: str = HEAD_SHA,
    previous_commit_sha: str | None = BASE_SHA,
) -> UUID:
    return await connection.fetchval(
        INSERT_RELEASE_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        environment_id,
        repository_id,
        name,
        commit_sha,
        previous_commit_sha,
        status,
        deployed_at,
    )


async def cut(releases, **overrides):
    """Cut a release through the real service, with the fixture's defaults."""
    arguments = {
        "scope": BOOTSTRAP_SCOPE,
        "name": "v1.4.0",
        "environment_id": BOOTSTRAP_ENVIRONMENT_ID,
        "repository_id": BOOTSTRAP_REPOSITORY_ID,
        "commit_sha": HEAD_SHA,
        "previous_commit_sha": BASE_SHA,
    }

    return await releases.create(**(arguments | overrides))


def field_codes(raised) -> list[tuple[str, str]]:
    return [(issue.field, issue.code) for issue in raised.value.issues]


# --- the vocabularies the application also holds ----------------------


async def test_the_environment_kind_constraint_and_the_domain_tuple_agree(connection):
    """`environments_kind_check` and ENVIRONMENT_KINDS are two statements of
    one rule, and the service refuses an unknown kind without a round trip
    precisely because it trusts its own copy."""
    definition = await connection.fetchval(
        CONSTRAINT_DEFINITION_SQL, "environments_kind_check"
    )

    for kind in ENVIRONMENT_KINDS:
        assert f"'{kind}'" in definition

    # And nothing else: counting is what catches an ADDITION rather than only a
    # removal.
    assert definition.count("'") == 2 * len(ENVIRONMENT_KINDS)


async def test_the_release_status_constraint_and_the_domain_tuple_agree(connection):
    definition = await connection.fetchval(
        CONSTRAINT_DEFINITION_SQL, "releases_status_check"
    )

    for status in RELEASE_STATUSES:
        assert f"'{status}'" in definition

    assert definition.count("'") == 2 * len(RELEASE_STATUSES)


# --- the ordinary path works ------------------------------------------


async def test_a_release_records_what_it_shipped_in_its_own_workspace(connection):
    """The feature, before the refusals: this is the row the whole file is
    about protecting, and it has to be storable or the rest proves nothing."""
    release_id = await insert_bootstrap_release(connection)

    await connection.execute(
        INSERT_RELEASE_ISSUE_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        release_id,
        C1_ISSUE_ID,
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM release_issues WHERE release_id = $1",
            release_id,
        )
        == 1
    )


# --- the cross-tenant refusals ----------------------------------------


async def test_a_release_cannot_deploy_to_another_workspaces_environment(connection):
    """One workspace_id for the row, so this pairing has nowhere to be spelled.

    The row claims to be in the bootstrap workspace and points at the other
    workspace's environment. releases_environment_fk reads the same
    workspace_id for both halves, so the pair simply does not exist.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await insert_bootstrap_release(connection, environment_id=OTHER_ENVIRONMENT_ID)

    assert raised.value.constraint_name == "releases_environment_fk"


async def test_a_release_cannot_name_another_workspaces_repository(connection):
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await insert_bootstrap_release(connection, repository_id=OTHER_REPOSITORY_ID)

    assert raised.value.constraint_name == "releases_repository_fk"


async def test_a_release_cannot_claim_to_have_shipped_another_workspaces_issue(
    connection,
):
    """The attack this migration exists for.

    The issues on a release are resolved from identifiers that arrived in
    commit messages and pull-request titles -- text written, on a public
    repository, by anybody. A resolver that forgot to scope its lookup would
    hand back another tenant's issue id, and the link row is then the leak: a
    release note is a document that gets pasted into a changelog and sent to
    customers, so it would publish somebody else's issue title.

    There is no column for the second workspace to go in, so the row is refused
    by the schema rather than by the resolver remembering.
    """
    release_id = await insert_bootstrap_release(connection)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_RELEASE_ISSUE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            release_id,
            OTHER_ISSUE_ID,
        )

    assert raised.value.constraint_name == "release_issues_issue_fk"


async def test_a_link_row_cannot_smuggle_a_second_workspace_through_its_own_id(
    connection,
):
    """The other direction: claim to BE the other workspace.

    A row whose workspace_id is the other tenant's, pointing at this tenant's
    release. It fails on the release key rather than the issue key, which is
    the point -- both halves are checked against the one column, so whichever
    way round an attacker spells the mismatch, one of the two constraints is
    looking at it.
    """
    release_id = await insert_bootstrap_release(connection)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_RELEASE_ISSUE_SQL,
            OTHER_WORKSPACE_ID,
            release_id,
            OTHER_ISSUE_ID,
        )

    assert raised.value.constraint_name == "release_issues_release_fk"


async def test_a_release_cannot_be_cut_from_another_workspaces_commit(releases):
    """The one id on a release that carries no foreign key, and therefore the
    one whose tenancy is this code's job.

    `releases.commit_sha` deliberately references nothing -- the migration
    argues for that at length -- so the guarantee is that
    `ReleaseRepository.find_commit` is scoped to (workspace, repository). A SHA
    that really exists, in a repository that really exists, in somebody else's
    workspace, comes back as NOT_FOUND: the same answer a SHA that exists
    nowhere gets, so nothing here is an oracle for what another tenant has
    deployed.
    """
    with pytest.raises(ValidationError) as raised:
        await cut(releases, commit_sha=OTHER_SHA, previous_commit_sha=None)

    assert field_codes(raised) == [("commitSha", "NOT_FOUND")]


async def test_an_unknown_commit_is_refused_the_same_way(releases):
    """The other half of the pair above. If a nonexistent SHA answered
    differently from another tenant's, the difference would be the oracle."""
    with pytest.raises(ValidationError) as raised:
        await cut(releases, commit_sha=sha("dead"), previous_commit_sha=None)

    assert field_codes(raised) == [("commitSha", "NOT_FOUND")]


async def test_a_release_cannot_be_cut_into_another_workspaces_environment(releases):
    """The composite key, reported as a field error rather than a 500.

    The environment exists and the SHA resolves, so this reaches the INSERT and
    is refused by `releases_environment_fk` -- which the service translates
    into the same NOT_FOUND an environment that never existed would produce.
    """
    with pytest.raises(ValidationError) as raised:
        await cut(releases, environment_id=OTHER_ENVIRONMENT_ID)

    assert field_codes(raised) == [("environmentId", "NOT_FOUND")]


# --- the state model --------------------------------------------------


async def test_a_deployed_release_must_carry_the_instant_it_deployed(connection):
    """ "What is running, and since when" is one question. A release reported as
    deployed with no instant would make the second half unanswerable."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_bootstrap_release(connection, status="deployed")

    assert raised.value.constraint_name == "releases_deployed_at_matches_status"


async def test_a_pending_release_must_not_carry_one(connection):
    """The same constraint from the other side, which is why it is an equality
    between two booleans rather than one implication: a release that has not
    deployed claiming a deploy instant is exactly as wrong."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_bootstrap_release(connection, status="pending", deployed_at=T3)

    assert raised.value.constraint_name == "releases_deployed_at_matches_status"


async def test_a_rolled_back_release_keeps_the_instant_it_deployed(connection):
    """A withdrawn release WAS deployed, and the instant is what an incident
    review reads. `rolled_back` is therefore on the left-hand side of the
    equality with `deployed`."""
    release_id = await insert_bootstrap_release(
        connection, status="rolled_back", deployed_at=T3
    )

    assert (
        await connection.fetchval(
            "SELECT deployed_at FROM releases WHERE id = $1", release_id
        )
        == T3
    )


async def test_a_range_cannot_start_and_end_at_one_commit(connection):
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_bootstrap_release(connection, previous_commit_sha=HEAD_SHA)

    assert raised.value.constraint_name == "releases_previous_commit_differs"


async def test_an_abbreviated_sha_is_refused(connection):
    """The range is resolved by looking this value up by equality, so an
    abbreviation would not be a slightly worse key -- it would silently match
    nothing and produce a release whose notes are permanently empty."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_bootstrap_release(connection, commit_sha="a39fd12")

    assert raised.value.constraint_name == "releases_commit_sha_format"


async def test_two_environments_in_one_workspace_cannot_share_a_name(connection):
    """Whichever one a deploy script picked would be the one nobody was
    watching."""
    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(
            INSERT_ENVIRONMENT_SQL,
            UUID("00000000-0000-7000-8000-0000000000df"),
            BOOTSTRAP_WORKSPACE_ID,
            "Production",
            "production",
        )

    assert raised.value.constraint_name == "environments_workspace_name_key"


async def test_two_workspaces_may_each_have_a_production(connection):
    """The uniqueness is per workspace, which the seed already relies on -- and
    asserting it keeps the constraint from being tightened into a global one by
    somebody reading only its name."""
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM environments WHERE name = 'Production'"
        )
        == 2
    )


async def test_an_invented_environment_kind_is_refused(connection):
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_ENVIRONMENT_SQL,
            UUID("00000000-0000-7000-8000-0000000000de"),
            BOOTSTRAP_WORKSPACE_ID,
            "Preview",
            "preview",
        )

    assert raised.value.constraint_name == "environments_kind_check"


# --- what a delete must not silently do -------------------------------


async def test_deleting_a_release_does_not_silently_discard_what_it_shipped(
    connection,
):
    """RESTRICT, not CASCADE, for the reason 017 gives about its own join
    tables: a one-line delete must not discard rows in another table while
    reporting `DELETE 1`. ReleaseService.delete removes them itself, first, in
    the same transaction.

    `RestrictViolationError`, not `ForeignKeyViolationError`. The two are
    siblings under IntegrityConstraintViolationError rather than one being the
    other's parent, and PostgreSQL raises them for opposite situations:
    catching the wrong one here would pass for a schema with no constraint at
    all, since the delete would then simply succeed and raise nothing.
    """
    release_id = await insert_bootstrap_release(connection)
    await connection.execute(
        INSERT_RELEASE_ISSUE_SQL, BOOTSTRAP_WORKSPACE_ID, release_id, C1_ISSUE_ID
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute("DELETE FROM releases WHERE id = $1", release_id)


async def test_removing_an_environment_does_not_silently_discard_its_history(
    connection,
):
    """Deleting a deploy target must not take everything ever deployed to it."""
    await insert_bootstrap_release(connection)

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM environments WHERE id = $1", BOOTSTRAP_ENVIRONMENT_ID
        )


async def test_a_release_holds_its_repository_against_a_disconnect(connection):
    """The consequence the migration writes down rather than leaves to be
    discovered: a workspace holding releases cannot drop a covered repository,
    because `GithubService.disconnect` deletes `github_repositories` rows and
    this constraint refuses while a release names one.

    Asserted because it is a real coupling between two features, not because it
    is convenient -- and `releaseDelete` is the documented way out of it.
    """
    await insert_bootstrap_release(connection)

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM github_repositories "
            "WHERE workspace_id = $1 AND repository_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
        )


# --- the range resolves the issues touched inside it ------------------


async def test_a_release_resolves_exactly_the_issues_inside_its_range(releases):
    """The core reading, with a row on each side of each boundary.

    `(BASE, HEAD]` is half-open at the bottom and closed at the top, so the
    base commit's own issue is excluded and the head commit's is included --
    which is what makes consecutive releases partition the history instead of
    overlapping at every boundary.

    The pull-request-only issue is in here too, and it is the reason
    `resolve_range` UNIONs two sources: nothing in any commit message names
    CORE-5. Taking only the commit side would drop the link source 017
    documents as the most common one.

    `CORE` is the bootstrap team's key, seeded by migrations/002_tenancy.sql
    and 005; the identifiers in the note are built from it and the issue
    numbers above.
    """
    release = await cut(releases)

    assert set(release.issue_ids) == {
        C1_ISSUE_ID,
        C2_ISSUE_ID,
        HEAD_ISSUE_ID,
        PULL_ONLY_ISSUE_ID,
    }
    assert BEFORE_ISSUE_ID not in release.issue_ids
    assert AFTER_ISSUE_ID not in release.issue_ids


async def test_a_release_lists_only_the_pull_requests_merged_inside_its_range(
    releases,
):
    """#7 merged at the lower boundary itself and is excluded; #84 merged
    inside and is not."""
    release = await cut(releases)

    assert list(release.pull_request_numbers) == [84]


async def test_the_notes_quote_the_issues_and_pull_requests_the_range_held(
    releases,
):
    """The document, not just the links. Everything in it came out of migration
    017's tables and nothing came from the caller except the name."""
    release = await cut(releases)

    assert "CORE-2 Landed on day one" in release.notes
    assert "CORE-5 Linked only by a pull request title" in release.notes
    assert "#84 CORE-5 Fix the OAuth callback" in release.notes

    assert "Shipped before the range" not in release.notes
    assert "Landed after the range" not in release.notes
    assert f"Range: {BASE_SHA[:7]}..{HEAD_SHA[:7]}" in release.notes


async def test_a_first_release_with_no_lower_bound_covers_everything_behind_it(
    releases,
):
    """`previousCommitSha: null` is the first deploy of a repository into an
    environment: no lower bound, so the base commit's issue is in."""
    release = await cut(releases, previous_commit_sha=None)

    assert BEFORE_ISSUE_ID in release.issue_ids
    assert AFTER_ISSUE_ID not in release.issue_ids
    assert set(release.pull_request_numbers) == {7, 84}


async def test_an_omitted_lower_bound_resumes_from_the_last_deploy(releases):
    """The value a pipeline would otherwise have to remember client-side.

    The first release covers everything up to C1 and is marked deployed; the
    second omits `previousCommitSha` entirely and must resume from C1 rather
    than starting again at the beginning. That lookup is
    `previous_deployed_commit_sha`, and it keys on `deployed_at IS NOT NULL`
    for the reason a rollback gives.
    """
    first = await cut(
        releases, name="v1.3.0", commit_sha=C1_SHA, previous_commit_sha=None
    )
    await releases.set_status(
        scope=BOOTSTRAP_SCOPE, release_id=first.id, status="deployed"
    )

    second = await cut(releases, commit_sha=HEAD_SHA, previous_commit_sha=UNSET)

    assert second.previous_commit_sha == C1_SHA
    assert BEFORE_ISSUE_ID not in second.issue_ids
    assert C1_ISSUE_ID not in second.issue_ids
    assert {C2_ISSUE_ID, HEAD_ISSUE_ID} <= set(second.issue_ids)


async def test_a_failed_release_does_not_move_the_next_ones_starting_point(
    releases,
):
    """A failed deploy shipped nothing, so its range is still unaccounted for
    and the next release has to cover it. `deployed_at IS NOT NULL` is what
    skips it -- exactly the left-hand side of
    `releases_deployed_at_matches_status`, so the two cannot drift.
    """
    failed = await cut(
        releases, name="v1.3.0", commit_sha=C1_SHA, previous_commit_sha=None
    )
    await releases.set_status(
        scope=BOOTSTRAP_SCOPE, release_id=failed.id, status="failed"
    )

    second = await cut(releases, commit_sha=HEAD_SHA, previous_commit_sha=UNSET)

    assert second.previous_commit_sha is None
    assert C1_ISSUE_ID in second.issue_ids


async def test_a_lower_bound_newer_than_the_head_is_refused(releases):
    """An empty window silently renders an empty note, which reads as "this
    deploy changed nothing" rather than as "you passed the ends the wrong way
    round"."""
    with pytest.raises(ValidationError) as raised:
        await cut(releases, commit_sha=C1_SHA, previous_commit_sha=HEAD_SHA)

    assert field_codes(raised) == [("previousCommitSha", "OUT_OF_ORDER")]


async def test_a_range_larger_than_the_limit_says_so_rather_than_omitting_quietly(
    connection, releases
):
    """The bound, and the disclaimer that makes it honest.

    A first release of a busy repository is the ordinary way to ask for more
    changes than one document can hold, and the two failures available are a
    refusal (which blocks a real deploy) and a silent truncation (which ships a
    changelog that reads as complete). Neither is acceptable, so the service
    reads RANGE_LIMIT + 1 rows to learn that there were more, freezes the first
    RANGE_LIMIT, and says so in the note.

    Both halves are asserted, because they can disagree: the note is rendered
    from one list and the link rows are written from another, and a trim
    applied to only one of them is the inconsistency this whole design exists
    to prevent.
    """
    extra = RANGE_LIMIT + 1

    await connection.executemany(
        INSERT_ISSUE_SQL,
        [
            (
                UUID(int=0x9000 + index),
                BOOTSTRAP_WORKSPACE_ID,
                BOOTSTRAP_TEAM_ID,
                1000 + index,
                f"Bulk change {index}",
            )
            for index in range(extra)
        ],
    )
    await connection.executemany(
        INSERT_COMMIT_SQL,
        [
            (
                BOOTSTRAP_WORKSPACE_ID,
                BOOTSTRAP_REPOSITORY_ID,
                sha(f"f{index:06x}"),
                f"Bulk commit {index}",
                T1 + timedelta(seconds=index),
            )
            for index in range(extra)
        ],
    )
    await connection.executemany(
        INSERT_COMMIT_LINK_SQL,
        [
            (
                BOOTSTRAP_WORKSPACE_ID,
                BOOTSTRAP_REPOSITORY_ID,
                sha(f"f{index:06x}"),
                UUID(int=0x9000 + index),
            )
            for index in range(extra)
        ],
    )

    release = await cut(releases)

    assert TRUNCATED in release.notes
    assert len(release.issue_ids) == RANGE_LIMIT

    stored = await connection.fetchval(
        "SELECT count(*) FROM release_issues WHERE release_id = $1", release.id
    )

    assert stored == RANGE_LIMIT


# --- the notes are deterministic, through the real repository ---------


async def test_two_releases_over_one_range_render_identical_notes(releases):
    """The guarantee, driven by the real statements against the real schema.

    tests/test_releases.py proves it of the pure function by shuffling its
    inputs. This is the half that function cannot prove about itself: that the
    repository hands it the same SET each time, and that nothing between the
    two -- an ORDER BY, a plan, a dictionary iteration order -- has crept in as
    the only reason the bytes matched.

    Two separate releases rather than one read twice, deliberately: reading one
    row twice would pass against a renderer that was called once and stored.
    """
    first = await cut(releases, name="v1.4.0")
    second = await cut(releases, name="v1.4.0")

    assert first.id != second.id
    assert first.notes == second.notes


async def test_the_stored_note_is_frozen_against_later_activity(connection, releases):
    """The reason the note is a column and not a view.

    A pull-request title is edited after the release is cut -- an ordinary
    webhook, not an attack -- and the release note must go on saying what it
    said. A note computed on read would answer differently, so the document
    that went out and the document the API returns would drift.
    """
    release = await cut(releases)
    before = release.notes

    await connection.execute(
        "UPDATE github_pull_requests SET title = 'Rewritten later' "
        "WHERE workspace_id = $1 AND repository_id = $2 AND number = 84",
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_REPOSITORY_ID,
    )

    reread = await releases.get_by_id(scope=BOOTSTRAP_SCOPE, release_id=release.id)

    assert reread is not None
    assert reread.notes == before
    assert "Rewritten later" not in reread.notes


# --- the lifecycle ----------------------------------------------------


async def test_deploying_a_release_stamps_the_instant(releases):
    deployed = await releases.set_status(
        scope=BOOTSTRAP_SCOPE,
        release_id=(await cut(releases)).id,
        status="deployed",
    )

    assert deployed.status == "deployed"
    assert deployed.deployed_at is not None


async def test_rolling_back_keeps_the_instant_it_deployed(releases):
    release = await cut(releases)
    deployed = await releases.set_status(
        scope=BOOTSTRAP_SCOPE, release_id=release.id, status="deployed"
    )
    withdrawn = await releases.set_status(
        scope=BOOTSTRAP_SCOPE, release_id=release.id, status="rolled_back"
    )

    assert withdrawn.status == "rolled_back"
    assert withdrawn.deployed_at == deployed.deployed_at


async def test_a_release_that_never_deployed_cannot_be_rolled_back(releases):
    """The transition table, enforced as part of the UPDATE. The message names
    the state the release is actually in, because that is the one thing a
    client cannot work out from the refusal alone."""
    release = await cut(releases)

    with pytest.raises(ValidationError) as raised:
        await releases.set_status(
            scope=BOOTSTRAP_SCOPE, release_id=release.id, status="rolled_back"
        )

    assert field_codes(raised) == [("status", "INVALID_TRANSITION")]
    assert "pending" in raised.value.issues[0].message


async def test_a_terminal_release_stays_terminal(releases):
    release = await cut(releases)
    await releases.set_status(
        scope=BOOTSTRAP_SCOPE, release_id=release.id, status="failed"
    )

    with pytest.raises(ValidationError) as raised:
        await releases.set_status(
            scope=BOOTSTRAP_SCOPE, release_id=release.id, status="deployed"
        )

    assert field_codes(raised) == [("status", "INVALID_TRANSITION")]


async def test_another_workspaces_release_is_simply_not_found(releases):
    """A release that exists, in a workspace the caller did not name, answers
    exactly as an id that exists nowhere -- so a status mutation cannot be used
    to probe for one."""
    release = await cut(releases)
    elsewhere = WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID)

    assert await releases.get_by_id(scope=elsewhere, release_id=release.id) is None

    with pytest.raises(ValidationError) as raised:
        await releases.set_status(
            scope=elsewhere, release_id=release.id, status="deployed"
        )

    assert field_codes(raised) == [("id", "NOT_FOUND")]


# --- deleting ---------------------------------------------------------


async def test_deleting_a_release_clears_its_links_first(connection, releases):
    """Both foreign keys onto `releases` are RESTRICT, so the service has to
    remove the link rows itself -- in one transaction, so a failure part way
    leaves the release intact rather than half dismantled."""
    release = await cut(releases)

    await releases.delete(scope=BOOTSTRAP_SCOPE, release_id=release.id)

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM release_issues WHERE release_id = $1", release.id
        )
        == 0
    )
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM releases WHERE id = $1", release.id
        )
        == 0
    )


async def test_deleting_another_workspaces_release_reports_not_found(releases):
    release = await cut(releases)

    with pytest.raises(ValidationError) as raised:
        await releases.delete(
            scope=WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID),
            release_id=release.id,
        )

    assert field_codes(raised) == [("id", "NOT_FOUND")]
