"""The development statements, run against a real schema by the real service.

tests/test_migration_017_db.py asks what the schema refuses. This asks whether
the application ever gets that far: it applies every migration, wires
`GithubService` over a real pool, and feeds it webhook payloads.

The two are not the same test and neither replaces the other. The schema's
composite foreign keys make a cross-tenant link unstorable; what this file
pins is that the resolver never finds the id in the first place -- because a
delivery that ends in a ForeignKeyViolationError is a 500, and a 500 is an
infinite redelivery loop. "Refused by the database" is the floor, not the
behaviour.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from app.domain.tenancy import AuthorizedWorkspaceScope, WorkspaceScope
from app.repositories.github import GithubRepository
from app.services.github import GithubAppConfig, GithubService

from tests.conftest import (
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

# The tenant 002 seeds.
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# The other tenant, real in every respect -- team, member, installation,
# repository, issue -- so that every cross-tenant assertion below fails for
# the reason it names rather than because the target was missing.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b2")
ARCHIVED_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b3")

INSTALLATION_ID = 4242
OTHER_INSTALLATION_ID = 5353

REPOSITORY_ID = 11111
OTHER_REPOSITORY_ID = 22222

PULL_NUMBER = 84
SHA = "a39fd12" + "0" * 33

EARLIER = "2026-04-01T09:00:00Z"
LATER = "2026-04-01T10:00:00Z"

# The bootstrap team's key, assigned by 005 rather than chosen here. Its
# issue is therefore CORE-142; the other tenant's team is OTH and its issue is
# OTH-142, deliberately sharing a NUMBER so that a resolver keyed on the number
# alone -- or on the key without the workspace -- would hand back the wrong one.
TEAM_KEY = "CORE"
OTHER_TEAM_KEY = "OTH"
ISSUE_NUMBER = 142

SCOPE = WorkspaceScope(workspace_id=WORKSPACE_ID)

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, number, workflow_state_id, title, priority,
    archived_at
)
VALUES (
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
    ),
    $5, 1, $6
)
"""


@pytest.fixture
async def pool(postgres_dsn):
    """A pool over a fully migrated database with two populated tenants."""
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)
        await seed(connection)
    finally:
        await connection.close()

    created = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield created
    finally:
        await created.close()

        cleanup = await asyncpg.connect(postgres_dsn)

        try:
            await reset_schema(cleanup)
        finally:
            await cleanup.close()


async def seed(connection) -> None:
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        OTHER_WORKSPACE_ID,
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, 'Other', $3)",
        OTHER_TEAM_ID,
        OTHER_WORKSPACE_ID,
        OTHER_TEAM_KEY,
    )
    await seed_workflow_states(connection, OTHER_WORKSPACE_ID, OTHER_TEAM_ID)

    for user_id in (MEMBER_ID, OUTSIDER_ID):
        await connection.execute(INSERT_USER_SQL, user_id)

    for workspace_id, user_id, installation_id in (
        (WORKSPACE_ID, MEMBER_ID, INSTALLATION_ID),
        (OTHER_WORKSPACE_ID, OUTSIDER_ID, OTHER_INSTALLATION_ID),
    ):
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, 'admin')",
            workspace_id,
            user_id,
        )
        # Confirmed on the way in: an unconfirmed claim resolves no delivery,
        # and 016 refuses an account login on one anyway.
        await connection.execute(
            "INSERT INTO github_installations "
            "(workspace_id, installation_id, connected_by, confirmed_at) "
            "VALUES ($1, $2, $3, now())",
            workspace_id,
            installation_id,
            user_id,
        )

    for workspace_id, repository_id, full_name in (
        (WORKSPACE_ID, REPOSITORY_ID, "acme/vector"),
        (OTHER_WORKSPACE_ID, OTHER_REPOSITORY_ID, "other/secret"),
    ):
        await connection.execute(
            "INSERT INTO github_repositories (workspace_id, repository_id, full_name) "
            "VALUES ($1, $2, $3)",
            workspace_id,
            repository_id,
            full_name,
        )

    for issue_id, workspace_id, team_id, number, title, archived_at in (
        (ISSUE_ID, WORKSPACE_ID, TEAM_ID, ISSUE_NUMBER, "Ours", None),
        (
            OTHER_ISSUE_ID,
            OTHER_WORKSPACE_ID,
            OTHER_TEAM_ID,
            ISSUE_NUMBER,
            "Theirs",
            None,
        ),
        (ARCHIVED_ISSUE_ID, WORKSPACE_ID, TEAM_ID, 900, "Archived", "now()"),
    ):
        await connection.execute(
            INSERT_ISSUE_SQL.replace("$6", "now()" if archived_at else "NULL"),
            issue_id,
            workspace_id,
            team_id,
            number,
            title,
        )


def build_service(pool) -> GithubService:
    """The real service over the real repository and the real pool.

    The config is a fully-populated fake because nothing on these paths reads
    a credential: `apply_webhook` is reached after the signature is verified,
    and the signature is verified in the transport. What the config decides
    here is only `configured`, which `disconnect` needs to answer a status.
    """
    return GithubService(
        pool=pool,
        repository=GithubRepository(),
        config=GithubAppConfig(
            app_id="123456",
            private_key="not-a-key",
            webhook_secret="not-a-secret",
            client_id="not-a-client",
            client_secret="not-a-secret-either",
            redirect_allowlist=(),
        ),
    )


def pull_payload(
    *,
    action="opened",
    title="Fix the callback",
    body=None,
    head_ref=None,
    state="open",
    draft=False,
    merged_at=None,
    updated_at=LATER,
    installation_id=INSTALLATION_ID,
    repository_id=REPOSITORY_ID,
):
    return {
        "action": action,
        "installation": {"id": installation_id},
        "repository": {"id": repository_id, "full_name": "acme/vector"},
        "pull_request": {
            "number": PULL_NUMBER,
            "title": title,
            "body": body,
            "state": state,
            "draft": draft,
            "merged_at": merged_at,
            "updated_at": updated_at,
            "head": {"ref": head_ref},
            "html_url": f"https://github.com/acme/vector/pull/{PULL_NUMBER}",
        },
    }


def push_payload(*, message, sha=SHA, repository_id=REPOSITORY_ID):
    return {
        "ref": "refs/heads/main",
        "installation": {"id": INSTALLATION_ID},
        "repository": {"id": repository_id, "full_name": "acme/vector"},
        "commits": [{"id": sha, "message": message, "timestamp": LATER}],
    }


async def links(pool, *, source=None):
    rows = await pool.fetch(
        "SELECT workspace_id, issue_id, source FROM github_pull_request_issues "
        "WHERE $1::text IS NULL OR source = $1",
        source,
    )

    return {(row["workspace_id"], row["issue_id"], row["source"]) for row in rows}


# --- the ordinary path, through the real statements --------------------


async def test_a_signed_pull_request_delivery_writes_a_row_and_its_links(pool):
    """The feature end to end: payload in, rows out, nothing hand-written.

    Asserted before every refusal below, because a refusal proves nothing
    against a code path that never worked.
    """
    await build_service(pool).apply_webhook(
        event="pull_request",
        payload=pull_payload(
            title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}",
            body=f"more about {TEAM_KEY}-{ISSUE_NUMBER}",
            head_ref=f"core-{ISSUE_NUMBER}-fix",
        ),
        delivery_id="d-1",
    )

    row = await pool.fetchrow(
        "SELECT * FROM github_pull_requests WHERE workspace_id = $1", WORKSPACE_ID
    )

    assert row["repository_id"] == REPOSITORY_ID
    assert row["number"] == PULL_NUMBER
    assert row["state"] == "open"
    assert row["head_ref"] == f"core-{ISSUE_NUMBER}-fix"
    assert row["github_updated_at"].isoformat() == "2026-04-01T10:00:00+00:00"

    assert await links(pool) == {
        (WORKSPACE_ID, ISSUE_ID, "title"),
        (WORKSPACE_ID, ISSUE_ID, "body"),
        (WORKSPACE_ID, ISSUE_ID, "branch"),
    }


async def test_a_push_writes_its_commits_and_links(pool):
    await build_service(pool).apply_webhook(
        event="push",
        payload=push_payload(message=f"{TEAM_KEY}-{ISSUE_NUMBER} handle the redirect"),
        delivery_id="d-1",
    )

    row = await pool.fetchrow("SELECT * FROM github_commits")

    assert row["sha"] == SHA
    assert row["workspace_id"] == WORKSPACE_ID
    assert await pool.fetchval("SELECT issue_id FROM github_commit_issues") == ISSUE_ID


async def test_the_development_read_returns_what_the_deliveries_wrote(pool):
    """Both list statements, including the array_agg over `source`.

    The aggregate is the part a fake cannot check: a pull request linked by
    two sources must come back as ONE row with two sources rather than twice.
    """
    service = build_service(pool)

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}",
            head_ref=f"core-{ISSUE_NUMBER}-fix",
        ),
        delivery_id="d-1",
    )
    await service.apply_webhook(
        event="push",
        payload=push_payload(message=f"{TEAM_KEY}-{ISSUE_NUMBER} done"),
        delivery_id="d-2",
    )

    view = await service.development_for_issue(
        SCOPE,
        issue_id=ISSUE_ID,
        identifier=f"{TEAM_KEY}-{ISSUE_NUMBER}",
        title="Fix Slack OAuth callback",
    )

    assert view.branch_name == "core-142-fix-slack-oauth-callback"

    (pull,) = view.pull_requests

    assert pull.repository_full_name == "acme/vector"
    assert pull.number == PULL_NUMBER
    assert pull.display_state == "open"
    assert pull.link_sources == ("branch", "title")

    (commit,) = view.commits

    assert commit.sha == SHA
    assert commit.short_sha == SHA[:7]
    assert commit.repository_full_name == "acme/vector"


# --- the cross-tenant refusal -----------------------------------------


async def test_a_title_naming_another_workspaces_issue_links_to_nothing(pool):
    """The attack, against the real resolver and the real schema.

    Both tenants have an issue numbered 142, and the pull request is on the
    bootstrap workspace's repository. A statement that resolved by number, or
    by key without the workspace, would find the other tenant's row -- and
    the delivery would then die on
    github_pull_request_issues_issue_fk, which is a 500 and an infinite
    redelivery loop rather than a clean refusal.

    So the assertion is BOTH that no link exists and that no exception
    escaped: the pull request itself is stored, because the delivery
    succeeded.
    """
    await build_service(pool).apply_webhook(
        event="pull_request",
        payload=pull_payload(
            title=f"Fixes {OTHER_TEAM_KEY}-{ISSUE_NUMBER}",
            body=f"{OTHER_TEAM_KEY}-{ISSUE_NUMBER}",
            head_ref=f"oth-{ISSUE_NUMBER}",
        ),
        delivery_id="d-1",
    )

    assert await links(pool) == set()
    assert await pool.fetchval("SELECT count(*) FROM github_pull_requests") == 1


async def test_a_commit_message_naming_another_workspaces_issue_links_to_nothing(pool):
    await build_service(pool).apply_webhook(
        event="push",
        payload=push_payload(message=f"{OTHER_TEAM_KEY}-{ISSUE_NUMBER} fix"),
        delivery_id="d-1",
    )

    assert await pool.fetchval("SELECT count(*) FROM github_commit_issues") == 0
    assert await pool.fetchval("SELECT count(*) FROM github_commits") == 1


async def test_the_resolver_answers_only_within_the_scope_it_was_given(pool):
    """The statement on its own, both directions.

    `resolve_issue_ids` is the one place an identifier becomes an id, so it is
    asserted directly as well as through the delivery: the same identifier
    resolves under the workspace that owns it and to nothing under the one
    that does not.
    """
    repository = GithubRepository()

    async with pool.acquire() as connection:
        mine = await repository.resolve_issue_ids(
            connection,
            scope=SCOPE,
            identifiers=[(TEAM_KEY, ISSUE_NUMBER), (OTHER_TEAM_KEY, ISSUE_NUMBER)],
        )
        theirs = await repository.resolve_issue_ids(
            connection,
            scope=WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID),
            identifiers=[(TEAM_KEY, ISSUE_NUMBER), (OTHER_TEAM_KEY, ISSUE_NUMBER)],
        )

    assert mine == {(TEAM_KEY, ISSUE_NUMBER): ISSUE_ID}
    assert theirs == {(OTHER_TEAM_KEY, ISSUE_NUMBER): OTHER_ISSUE_ID}


async def test_an_archived_issue_is_not_a_way_back_into_the_product(pool):
    """`archived_at IS NULL`, for the reason every issue read carries it: a
    pull-request title is not the way back in."""
    repository = GithubRepository()

    async with pool.acquire() as connection:
        assert (
            await repository.resolve_issue_ids(
                connection, scope=SCOPE, identifiers=[(TEAM_KEY, 900)]
            )
            == {}
        )


async def test_a_delivery_for_a_repository_this_workspace_lacks_is_dropped(pool):
    """The check that stops a foreign-key violation becoming a 500.

    `github_repositories_repository_id_idx` is deliberately not unique, so the
    other tenant's repository id is a real row somewhere -- just not here.
    """
    await build_service(pool).apply_webhook(
        event="pull_request",
        payload=pull_payload(
            title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}",
            repository_id=OTHER_REPOSITORY_ID,
        ),
        delivery_id="d-1",
    )

    assert await pool.fetchval("SELECT count(*) FROM github_pull_requests") == 0


# --- idempotency, against the real primary key -------------------------


async def test_a_redelivery_is_recorded_once_and_applied_once(pool):
    service = build_service(pool)
    payload = push_payload(message=f"{TEAM_KEY}-{ISSUE_NUMBER} done")

    await service.apply_webhook(event="push", payload=payload, delivery_id="d-1")
    await service.apply_webhook(event="push", payload=payload, delivery_id="d-1")

    assert await pool.fetchval("SELECT count(*) FROM github_deliveries") == 1
    assert await pool.fetchval("SELECT count(*) FROM github_commits") == 1


async def test_a_delivery_that_fails_is_not_recorded_as_applied(pool):
    """The reason the claim is inside the transaction rather than before it.

    A delivery whose writes roll back must leave no delivery row, or GitHub's
    retry -- the one chance to apply it -- would be discarded as a duplicate.
    Forced here by making the write fail: a title of 2000 characters is
    clamped by the service, so the failure has to come from somewhere the
    service does not clamp, and an unparseable payload would simply be
    ignored. The honest way to provoke it is to break the statement itself.
    """
    service = build_service(pool)

    async def explode(*args, **kwargs):
        raise asyncpg.PostgresError("statement failed")

    service._repository.upsert_pull_request = explode

    with pytest.raises(asyncpg.PostgresError):
        await service.apply_webhook(
            event="pull_request",
            payload=pull_payload(title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}"),
            delivery_id="d-doomed",
        )

    assert await pool.fetchval("SELECT count(*) FROM github_deliveries") == 0


# --- out-of-order redelivery, against the ON CONFLICT WHERE -------------


async def test_an_out_of_order_redelivery_does_not_regress_the_row(pool):
    """The `WHERE` on the conflict branch, run for real.

    A read-then-write in Python would pass a single-threaded test and lose to
    a concurrent delivery; this is the statement deciding.
    """
    service = build_service(pool)

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="closed",
            state="closed",
            merged_at=LATER,
            title="Fix the callback",
            updated_at=LATER,
        ),
        delivery_id="d-later",
    )
    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="opened", state="open", title="WIP", updated_at=EARLIER
        ),
        delivery_id="d-earlier",
    )

    row = await pool.fetchrow(
        "SELECT state, merged_at, title FROM github_pull_requests"
    )

    assert row["state"] == "closed"
    assert row["merged_at"] is not None
    assert row["title"] == "Fix the callback"


async def test_a_stale_redelivery_leaves_the_links_alone(pool):
    service = build_service(pool)

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}", updated_at=LATER
        ),
        delivery_id="d-later",
    )
    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="No identifier", updated_at=EARLIER),
        delivery_id="d-earlier",
    )

    assert await links(pool) == {(WORKSPACE_ID, ISSUE_ID, "title")}


# --- editing, against the source-keyed primary key ---------------------


async def test_editing_a_title_retracts_its_link_and_not_the_branchs(pool):
    """Two rows in, one row out, because `source` is inside the key."""
    service = build_service(pool)

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}",
            head_ref=f"core-{ISSUE_NUMBER}-fix",
        ),
        delivery_id="d-1",
    )

    assert len(await links(pool)) == 2

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="edited",
            title="Fix the thing",
            head_ref=f"core-{ISSUE_NUMBER}-fix",
            updated_at="2026-04-01T11:00:00Z",
        ),
        delivery_id="d-2",
    )

    assert await links(pool) == {(WORKSPACE_ID, ISSUE_ID, "branch")}


async def test_a_surviving_link_keeps_the_moment_it_was_made(pool):
    """`NOT (issue_id = ANY(...))` rather than a blanket delete for the source.

    "Linked since" is a fact about the link, not about the last delivery that
    mentioned it.
    """
    service = build_service(pool)
    payload = pull_payload(title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}")

    await service.apply_webhook(
        event="pull_request", payload=payload, delivery_id="d-1"
    )

    first = await pool.fetchval("SELECT created_at FROM github_pull_request_issues")

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="edited",
            title=f"Still fixes {TEAM_KEY}-{ISSUE_NUMBER}",
            updated_at="2026-04-01T11:00:00Z",
        ),
        delivery_id="d-2",
    )

    assert await pool.fetchval("SELECT created_at FROM github_pull_request_issues") == (
        first
    )


# --- removal, against RESTRICT ----------------------------------------


async def test_an_uninstall_removes_everything_in_an_order_restrict_allows(pool):
    """Four RESTRICT constraints between the link rows and the installation.

    Any wrong order here is a RestrictViolationError from PostgreSQL rather
    than a silently-passing test, which is exactly why 017 chose RESTRICT.
    """
    service = build_service(pool)

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}"),
        delivery_id="d-1",
    )
    await service.apply_webhook(
        event="push",
        payload=push_payload(message=f"{TEAM_KEY}-{ISSUE_NUMBER} done"),
        delivery_id="d-2",
    )
    await service.apply_webhook(
        event="installation",
        payload={"action": "deleted", "installation": {"id": INSTALLATION_ID}},
        delivery_id="d-3",
    )

    for table in (
        "github_pull_request_issues",
        "github_commit_issues",
        "github_pull_requests",
        "github_commits",
        "github_repositories",
        "github_installations",
    ):
        assert (
            await pool.fetchval(
                f"SELECT count(*) FROM {table} WHERE workspace_id = $1",
                WORKSPACE_ID,
            )
            == 0
        )

    # The other tenant is untouched, which is the half a `DELETE FROM` with a
    # forgotten predicate would get wrong.
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM github_installations WHERE workspace_id = $1",
            OTHER_WORKSPACE_ID,
        )
        == 1
    )


async def test_disconnecting_removes_the_development_history(pool):
    service = build_service(pool)

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}"),
        delivery_id="d-1",
    )

    integration = await service.disconnect(
        AuthorizedWorkspaceScope(
            workspace_id=WORKSPACE_ID, user_id=MEMBER_ID, role="admin"
        )
    )

    assert integration.status == "disconnected"
    assert await pool.fetchval("SELECT count(*) FROM github_pull_requests") == 0
    assert await pool.fetchval("SELECT count(*) FROM github_pull_request_issues") == 0


async def test_a_repository_removed_from_the_installation_takes_its_history(pool):
    service = build_service(pool)

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title=f"Fixes {TEAM_KEY}-{ISSUE_NUMBER}"),
        delivery_id="d-1",
    )
    await service.apply_webhook(
        event="installation_repositories",
        payload={
            "action": "removed",
            "installation": {"id": INSTALLATION_ID, "account": {"login": "acme"}},
            "repositories_removed": [{"id": REPOSITORY_ID, "full_name": "acme/vector"}],
        },
        delivery_id="d-2",
    )

    assert await pool.fetchval("SELECT count(*) FROM github_pull_requests") == 0
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM github_repositories WHERE workspace_id = $1",
            WORKSPACE_ID,
        )
        == 0
    )
