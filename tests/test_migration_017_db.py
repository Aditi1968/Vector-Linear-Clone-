"""Migration 017 applied by the real runner, then asked what it built.

017 stores development activity that arrives as *text somebody else wrote*. A
pull request title saying "ENG-142", a branch called "eng-142-fix", a commit
message mentioning it -- on a public repository every one of those is authored
by whoever opened the pull request, which is anybody at all. The identifier is
therefore a request to link and never a proof of one, and the question this
file exists to answer is what happens when that request names an issue in a
workspace the repository has nothing to do with.

The answer has to be the database, not the service. A resolver that scopes its
lookup correctly is right until someone writes a second one, and the second one
is where this class of bug lives. So the assertions below are mostly about rows
PostgreSQL will not store:

  * a pull request against another workspace's repository;
  * a pull-request link onto another workspace's issue -- the attack in the
    file's opening paragraph, refused by there being no column for a second
    workspace to go in;
  * the same for a commit;
  * `merged_at` on a pull request that is still open, which is not a state
    GitHub produces and would make the derived display state answer something
    no payload said;
  * a delivery applied twice.

And two that are about the shape being useful rather than safe: a title link
and a branch link to one issue are two rows (so editing a title cannot retract
what the branch still supports), and a full 40-character SHA is required (so
the key cannot be built on an abbreviation that collides by construction).

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

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

# The other tenant: a real workspace, with a real repository and a real issue,
# whose rows every cross-tenant assertion below tries and fails to reach. It
# has to be real -- a nonexistent id would be refused by any spelling of these
# constraints and would prove nothing about which one is in force.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

BOOTSTRAP_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b2")

BOOTSTRAP_REPOSITORY_ID = 11111
OTHER_REPOSITORY_ID = 22222

PULL_NUMBER = 84

# 40 lowercase hex characters, which is what github_commits_sha_format demands.
SHA = "a39fd12" + "0" * 33

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

# Confirmed on the way in. `github_repositories` hangs off the installation, so
# a test about pull requests needs one -- and 016 refuses an account login on a
# claim nobody confirmed, so the honest way to get a usable installation here is
# to date the confirmation rather than to leave the row half-built.
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

INSERT_PULL_REQUEST_SQL = """
INSERT INTO github_pull_requests
    (workspace_id, repository_id, number, title, state)
VALUES ($1, $2, $3, $4, $5)
"""

INSERT_PULL_LINK_SQL = """
INSERT INTO github_pull_request_issues
    (workspace_id, repository_id, number, issue_id, source)
VALUES ($1, $2, $3, $4, $5)
"""

INSERT_COMMIT_SQL = """
INSERT INTO github_commits (workspace_id, repository_id, sha, message)
VALUES ($1, $2, $3, $4)
"""

INSERT_COMMIT_LINK_SQL = """
INSERT INTO github_commit_issues (workspace_id, repository_id, sha, issue_id)
VALUES ($1, $2, $3, $4)
"""

INSERT_DELIVERY_SQL = """
INSERT INTO github_deliveries (delivery_id, event) VALUES ($1, $2)
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


async def seed(connection) -> None:
    """Two workspaces, each with a team, a member, a repository and an issue.

    Symmetric on purpose. Every cross-tenant assertion below is "workspace A's
    row pointing at workspace B's row", and a lopsided fixture -- where one
    side lacks the thing the other is reaching for -- would pass those
    assertions for the wrong reason.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        OTHER_WORKSPACE_ID,
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, 'Other', 'OTH')",
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

    for issue_id, workspace_id, team_id, title in (
        (BOOTSTRAP_ISSUE_ID, BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_TEAM_ID, "Ours"),
        (OTHER_ISSUE_ID, OTHER_WORKSPACE_ID, OTHER_TEAM_ID, "Theirs"),
    ):
        await connection.execute(
            INSERT_ISSUE_SQL, issue_id, workspace_id, team_id, 142, title
        )


async def insert_bootstrap_pull_request(connection, *, state: str = "open") -> None:
    await connection.execute(
        INSERT_PULL_REQUEST_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_REPOSITORY_ID,
        PULL_NUMBER,
        "Fix OAuth callback",
        state,
    )


# --- the ordinary path works ------------------------------------------


async def test_a_pull_request_links_to_an_issue_in_its_own_workspace(connection):
    """The feature, before the refusals: this is the row the whole file is
    about protecting, and it has to be storable or the rest proves nothing."""
    await insert_bootstrap_pull_request(connection)
    await connection.execute(
        INSERT_PULL_LINK_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_REPOSITORY_ID,
        PULL_NUMBER,
        BOOTSTRAP_ISSUE_ID,
        "title",
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM github_pull_request_issues WHERE issue_id = $1",
            BOOTSTRAP_ISSUE_ID,
        )
        == 1
    )


async def test_a_commit_links_to_an_issue_in_its_own_workspace(connection):
    await connection.execute(
        INSERT_COMMIT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_REPOSITORY_ID,
        SHA,
        "Handle redirect URI",
    )
    await connection.execute(
        INSERT_COMMIT_LINK_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_REPOSITORY_ID,
        SHA,
        BOOTSTRAP_ISSUE_ID,
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM github_commit_issues WHERE issue_id = $1",
            BOOTSTRAP_ISSUE_ID,
        )
        == 1
    )


# --- the cross-tenant refusals ----------------------------------------


async def test_a_pull_request_cannot_name_another_workspaces_repository(connection):
    """One workspace_id for the row, so this pairing has nowhere to be spelled.

    The row claims to be in the bootstrap workspace and points at the other
    workspace's repository id. github_pull_requests_repository_fk reads the
    same workspace_id for both halves, so the pair simply does not exist.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_PULL_REQUEST_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            OTHER_REPOSITORY_ID,
            PULL_NUMBER,
            "Fix OAuth callback",
            "open",
        )

    assert raised.value.constraint_name == "github_pull_requests_repository_fk"


async def test_a_pull_request_cannot_link_to_another_workspaces_issue(connection):
    """The attack this migration exists for.

    Someone opens a pull request on a repository their own workspace has
    connected, titles it with an identifier belonging to another tenant, and a
    resolver that forgot to scope its lookup hands back that tenant's issue id.
    The link row is then the leak: it would put a private issue into a
    Development panel, and -- once status automations exist -- let an outsider
    move somebody else's issue to Done by merging their own pull request.

    There is no column for the second workspace to go in, so the row is refused
    by the schema rather than by the resolver remembering.
    """
    await insert_bootstrap_pull_request(connection)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_PULL_LINK_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            PULL_NUMBER,
            OTHER_ISSUE_ID,
            "title",
        )

    assert raised.value.constraint_name == "github_pull_request_issues_issue_fk"


async def test_a_commit_cannot_link_to_another_workspaces_issue(connection):
    """The same attack through a commit message, refused the same way.

    Asserted separately rather than assumed from the pull-request case: these
    are two tables with two constraints, and the commit-side one is the easier
    of the pair to declare wrong, since its join carries no `source` column to
    make the shape look deliberate.
    """
    await connection.execute(
        INSERT_COMMIT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_REPOSITORY_ID,
        SHA,
        "Handle redirect URI",
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_COMMIT_LINK_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            SHA,
            OTHER_ISSUE_ID,
        )

    assert raised.value.constraint_name == "github_commit_issues_issue_fk"


async def test_a_link_row_cannot_smuggle_a_second_workspace_through_its_own_id(
    connection,
):
    """The other direction: claim to BE the other workspace.

    A row whose workspace_id is the other tenant's, pointing at this tenant's
    pull request. It fails on the pull-request key rather than the issue key,
    which is the point -- both halves are checked against the one column, so
    whichever way round an attacker spells the mismatch, one of the two
    constraints is looking at it.
    """
    await insert_bootstrap_pull_request(connection)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_PULL_LINK_SQL,
            OTHER_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            PULL_NUMBER,
            OTHER_ISSUE_ID,
            "title",
        )

    assert raised.value.constraint_name == (
        "github_pull_request_issues_pull_request_fk"
    )


# --- the state model --------------------------------------------------


async def test_an_open_pull_request_cannot_have_merged(connection):
    """state='open' with a merged_at is not a state GitHub produces.

    The display state (draft / open / merged / closed) is derived from these
    three columns, so a combination GitHub never sends would make that
    derivation answer something no payload said.
    """
    await insert_bootstrap_pull_request(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            "UPDATE github_pull_requests SET merged_at = now() "
            "WHERE workspace_id = $1 AND repository_id = $2 AND number = $3",
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            PULL_NUMBER,
        )

    assert raised.value.constraint_name == "github_pull_requests_merged_is_closed"


async def test_a_merged_pull_request_is_stored_as_closed_with_an_instant(connection):
    """The same constraint from the other side, so it is not merely a ban.

    This is what GitHub actually sends for a merge: state=closed, merged_at
    set. Both facts survive, which is what lets the domain tell a merged pull
    request from an abandoned one -- a single flattened enum could not.
    """
    await insert_bootstrap_pull_request(connection, state="closed")
    await connection.execute(
        "UPDATE github_pull_requests SET merged_at = now() "
        "WHERE workspace_id = $1 AND repository_id = $2 AND number = $3",
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_REPOSITORY_ID,
        PULL_NUMBER,
    )

    row = await connection.fetchrow(
        "SELECT state, merged_at FROM github_pull_requests "
        "WHERE workspace_id = $1 AND repository_id = $2 AND number = $3",
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_REPOSITORY_ID,
        PULL_NUMBER,
    )

    assert row["state"] == "closed"
    assert row["merged_at"] is not None


async def test_an_abbreviated_sha_is_refused(connection):
    """The key is the full SHA because the short form collides by construction.

    Seven characters is what the UI shows and what a commit message quotes, and
    a primary key built on it would start colliding in exactly the repositories
    large enough for the collision to matter.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_COMMIT_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            "a39fd12",
            "Handle redirect URI",
        )

    assert raised.value.constraint_name == "github_commits_sha_format"


# --- the source discriminator -----------------------------------------


async def test_a_title_link_and_a_branch_link_are_two_rows(connection):
    """`source` is inside the primary key, and that is what makes an edit safe.

    A pull request whose title and whose branch both name ENG-142 has two
    reasons to be linked. Editing the title retracts one of them; the branch
    still supports the other. Were `source` outside the key there would be one
    row, and removing the title's link would remove the branch's with it.
    """
    await insert_bootstrap_pull_request(connection)

    for source in ("title", "branch"):
        await connection.execute(
            INSERT_PULL_LINK_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            PULL_NUMBER,
            BOOTSTRAP_ISSUE_ID,
            source,
        )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM github_pull_request_issues WHERE issue_id = $1",
            BOOTSTRAP_ISSUE_ID,
        )
        == 2
    )


async def test_an_invented_source_is_refused(connection):
    """Three sources exist because three places can carry an identifier. A
    fourth is a typo in a writer, and a typo that reaches the table is a link
    whose evidence nothing can later weigh."""
    await insert_bootstrap_pull_request(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_PULL_LINK_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
            PULL_NUMBER,
            BOOTSTRAP_ISSUE_ID,
            "comment",
        )

    assert raised.value.constraint_name == "github_pull_request_issues_source_check"


# --- delivery idempotency ---------------------------------------------


async def test_a_delivery_id_can_only_be_recorded_once(connection):
    """GitHub retries a delivery it did not see a 2xx for.

    The retry has to be distinguishable from a first attempt before the payload
    is parsed, and a primary key is what makes the second one a refusal rather
    than a second application. Global rather than workspace-scoped
    deliberately: this check runs before the routing lookup that would say
    which workspace the delivery belongs to.
    """
    await connection.execute(INSERT_DELIVERY_SQL, "d-1", "pull_request")

    with pytest.raises(asyncpg.UniqueViolationError):
        await connection.execute(INSERT_DELIVERY_SQL, "d-1", "pull_request")


async def test_a_delivery_id_is_bounded(connection):
    """An unbounded TEXT primary key is a way to make an index enormous by
    sending a large header. GitHub sends 36 characters; 200 is the ceiling."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(INSERT_DELIVERY_SQL, "d" * 201, "push")

    assert raised.value.constraint_name == "github_deliveries_delivery_id_length"


# --- what a disconnect must not silently do ---------------------------


async def test_removing_a_repository_does_not_silently_discard_its_history(
    connection,
):
    """RESTRICT, not CASCADE, for the reason 013 gives about the repositories
    themselves: a one-line delete must not discard rows in another table while
    reporting `DELETE 1`. GithubService removes them itself, in order, in the
    same transaction.

    `RestrictViolationError`, not `ForeignKeyViolationError`. The two are
    siblings under IntegrityConstraintViolationError rather than one being the
    other's parent, and PostgreSQL raises them for opposite situations: the
    foreign-key error means a child pointed at a parent that was not there,
    while this one means the parent was there and declined to leave. Catching
    the wrong sibling here would pass for a schema that had no constraint at
    all, since the delete would then simply succeed and raise nothing.
    """
    await insert_bootstrap_pull_request(connection)

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM github_repositories "
            "WHERE workspace_id = $1 AND repository_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_REPOSITORY_ID,
        )
