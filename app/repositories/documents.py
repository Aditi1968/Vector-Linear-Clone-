import json
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.documents import (
    DocumentCommentEntity,
    DocumentContent,
    DocumentContentError,
    DocumentEntity,
    DocumentFilter,
    DocumentRevisionEntity,
    InvalidStoredContentError,
    parse_content,
    render_content,
)
from app.domain.tenancy import WorkspaceScope


# `list[...]` is not spellable inside the class below: it has a method called
# `list`, so from that `def` onwards the name in the class body IS the method
# and `-> list[X]` on a method below it is a subscript of a function. Aliases
# resolved out here, where `list` is still the builtin, fix both the import-time
# TypeError and mypy. app/repositories/projects.py states this in full.
Documents = list[DocumentEntity]
Revisions = list[DocumentRevisionEntity]
Comments = list[DocumentCommentEntity]
Contents = dict[UUID, DocumentContent]

# What a document read returns, and deliberately WITHOUT `content`. The body is
# fetched separately -- see `contents_for_documents` and `DocumentEntity` -- so
# a list of fifty documents is fifty rows of metadata rather than ten megabytes
# of prose nobody selected.
_DOCUMENT_COLUMNS = """
    id,
    title,
    project_id,
    initiative_id,
    creator_id,
    last_edited_by,
    created_at,
    updated_at
"""

# The same list, qualified. `restore` is an `UPDATE ... FROM`, so both tables
# are in scope for its RETURNING clause and every column name below exists in
# both of them -- an unqualified `id` there is an ambiguous reference and not a
# subtle bug but an outright error. The column ALIASES are unchanged, so the
# row this produces maps through `_to_document` like any other.
_DOCUMENT_COLUMNS_QUALIFIED = """
    documents.id,
    documents.title,
    documents.project_id,
    documents.initiative_id,
    documents.creator_id,
    documents.last_edited_by,
    documents.created_at,
    documents.updated_at
"""

_REVISION_COLUMNS = """
    id,
    document_id,
    title,
    author_id,
    created_at
"""

_COMMENT_COLUMNS = """
    id,
    document_id,
    author_id,
    body,
    edited_at,
    created_at,
    updated_at
"""


class DocumentRepository:
    """SQL access for `documents`, `document_revisions` and
    `document_comments`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a transaction.
    `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace and the scope arrives as a
    required keyword argument -- the shape IssueRepository establishes. That is
    what makes a read of another tenant's document return nothing rather than
    that tenant's writing: `workspace_id` is an equality in the WHERE clause,
    not a filter applied to rows already fetched.

    Nothing here checks a parent against its workspace before writing a row
    that references it. Every foreign key in migrations/023_documents.sql is
    composite over `workspace_id`, so PostgreSQL refuses a cross-workspace
    association as part of the statement itself; a SELECT-first check would be
    a second, weaker copy of that rule -- weaker because it is a separate
    statement the row can change between.

    ## The one thing this class does that a repository usually does not

    It PARSES the content it reads. `_to_content` runs
    `app.domain.documents.parse_content` over every stored tree on the way out,
    and raises `InvalidStoredContentError` when a row will not parse.

    That is not defensive decoration. A document's content is authored by
    whoever wrote the document, so a stored tree is client input wearing the
    costume of server state -- and unlike a stored filter, which ends up as
    parameters this server binds, a stored tree ends up as DOM in a reader's
    browser. The schema bounds it and checks that it is an object; everything
    else -- the node vocabulary, the attribute types, the link scheme -- is
    checked here, on the way to the client, so that a row written by a hand-run
    UPDATE, a bulk import or a future second writer is refused at this boundary
    rather than rendered past it.

    `restore` is the one path where content moves between two rows without
    passing through the parser, and it is safe for a stated reason rather than
    by omission: see that method.
    """

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
    ) -> DocumentEntity | None:
        """The document with this id in this workspace, or nothing.

        The workspace is part of the lookup rather than a check applied
        afterwards, so a document belonging to another tenant produces exactly
        the same answer as an id that exists nowhere. A caller holding a
        guessed or leaked id learns nothing by asking.
        """
        row = await connection.fetchrow(
            f"""
            SELECT {_DOCUMENT_COLUMNS}
            FROM documents
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            document_id,
        )

        if row is None:
            return None

        return self._to_document(row)

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_filter: DocumentFilter,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> Documents:
        """Keyset page of this workspace's documents, newest first.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison, and
        `workspace_id` leads every statement so a page is served by the leading
        columns of one of the three indexes 023 declares.

        The tenant predicate is ANDed with the cursor rather than folded into
        it. Widening the row-value comparison to
        `(workspace_id, created_at, id) < (...)` would put workspaces into the
        ordering, which is how a page walk falls out of one tenant and into
        whichever one sorts next. The parent filter is ANDed on for the same
        reason -- it NARROWS, and cannot substitute for the workspace.

        The filter clause is assembled from module-local literals; the only
        thing a caller contributes is a bound parameter. `IS NOT DISTINCT FROM`
        rather than `=` because `project_id=None` is a real filter -- "the
        documents belonging to no project" -- and `= NULL` matches nothing at
        all, which would silently answer that question with an empty page.
        """
        clause, filter_values = _filter_clause(document_filter, first_index=2)
        cursor_index = 2 + len(filter_values)

        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                f"""
                SELECT {_DOCUMENT_COLUMNS}
                FROM documents
                WHERE workspace_id = $1{clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ${cursor_index}
                """,
                scope.workspace_id,
                *filter_values,
                limit,
            )
        else:
            rows = await connection.fetch(
                f"""
                SELECT {_DOCUMENT_COLUMNS}
                FROM documents
                WHERE workspace_id = $1{clause}
                    AND (created_at, id) < (${cursor_index}, ${cursor_index + 1})
                ORDER BY created_at DESC, id DESC
                LIMIT ${cursor_index + 2}
                """,
                scope.workspace_id,
                *filter_values,
                after_created_at,
                after_id,
                limit,
            )

        return [self._to_document(row) for row in rows]

    async def contents_for_documents(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_ids: Sequence[UUID],
    ) -> Contents:
        """The parsed bodies of several documents, in one statement.

        Keyed by id and not returned as a list, because the caller is a
        DataLoader that has to answer one key at a time and an id missing from
        the result -- another workspace's document, or one that does not exist
        -- must be answerable as "nothing" rather than as a position in an
        array that has to line up.

        `= ANY($2::UUID[])` rather than a generated IN list, so the statement
        text does not vary with the batch size and asyncpg prepares it once.
        """
        rows = await connection.fetch(
            """
            SELECT id, content
            FROM documents
            WHERE workspace_id = $1 AND id = ANY($2::UUID[])
            """,
            scope.workspace_id,
            list(document_ids),
        )

        return {row["id"]: self._to_content(row["content"]) for row in rows}

    async def contents_for_revisions(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        revision_ids: Sequence[UUID],
    ) -> Contents:
        """The parsed bodies of several revisions, in one statement.

        The twin of `contents_for_documents`, and the parse matters at least as
        much: a revision is older than the rules that were in force when it was
        written, so a history entry is the likeliest place for a tree that no
        longer passes the parser to be sitting.
        """
        rows = await connection.fetch(
            """
            SELECT id, content
            FROM document_revisions
            WHERE workspace_id = $1 AND id = ANY($2::UUID[])
            """,
            scope.workspace_id,
            list(revision_ids),
        )

        return {row["id"]: self._to_content(row["content"]) for row in rows}

    async def list_revisions(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> Revisions:
        """Keyset page of one document's history, newest first.

        `(created_at DESC, id DESC)` and not `created_at` alone. Two revisions
        written in one transaction share a timestamp -- which `restore` does,
        every time -- and without the tie-break "which version came first"
        would depend on the scan order, and a page walk could repeat or skip
        one.

        A document in another workspace is not a separate case: the workspace
        equality already excludes every one of its revisions, so the page comes
        back empty rather than holding somebody else's history.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                f"""
                SELECT {_REVISION_COLUMNS}
                FROM document_revisions
                WHERE workspace_id = $1 AND document_id = $2
                ORDER BY created_at DESC, id DESC
                LIMIT $3
                """,
                scope.workspace_id,
                document_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                f"""
                SELECT {_REVISION_COLUMNS}
                FROM document_revisions
                WHERE workspace_id = $1
                    AND document_id = $2
                    AND (created_at, id) < ($3, $4)
                ORDER BY created_at DESC, id DESC
                LIMIT $5
                """,
                scope.workspace_id,
                document_id,
                after_created_at,
                after_id,
                limit,
            )

        return [self._to_revision(row) for row in rows]

    # ---------------------------------------------------------------- writes

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        title: str,
        content: DocumentContent,
        project_id: UUID | None,
        initiative_id: UUID | None,
        creator_id: UUID,
    ) -> DocumentEntity:
        """Insert one document into this workspace.

        `last_edited_by` is written equal to `creator_id` rather than left to a
        default, because there is no default that could be right: the column is
        NOT NULL and the person who made the document is the person who wrote
        what is in it. It is also the value the first edit's boundary rule
        compares against, so seeding it wrongly would make every document's
        first edit look like a hand-over.

        The content is rendered by `render_content` and cast, rather than passed
        as a dict: asyncpg encodes JSONB from `str` or `bytes` only -- a dict
        raises DataError -- and the explicit `::JSONB` is what stops the
        parameter being inferred as TEXT beside a JSONB column. The same
        arrangement `SavedViewRepository.create` uses for its filter.

        No parent is checked first. `documents_project_fk` and
        `documents_initiative_fk` are composite over this row's single
        `workspace_id`, so a project or an initiative from another tenant is
        refused as part of this statement, with no window between a check and
        a write.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO documents (
                workspace_id,
                title,
                content,
                project_id,
                initiative_id,
                creator_id,
                last_edited_by
            )
            VALUES ($1, $2, $3::JSONB, $4, $5, $6, $6)
            RETURNING {_DOCUMENT_COLUMNS}
            """,
            scope.workspace_id,
            title,
            render_content(content),
            project_id,
            initiative_id,
            creator_id,
        )

        return self._to_document(row)

    async def lock_for_edit(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
    ) -> DocumentEntity | None:
        """Take the document's row lock and read what the boundary rule needs.

        This is the whole of the revision guarantee's soundness, so it is worth
        being precise about what it does.

        `DocumentService.edit` decides whether to snapshot by comparing the
        incoming editor and the clock against `last_edited_by` and
        `updated_at`, and then writes both. Those are two statements, and
        between them a second edit could read the same "last edited by Ana ten
        minutes ago" and reach the same conclusion -- so both would decide no
        snapshot was needed, and one version would be overwritten with no
        record that it ever existed.

        `FOR UPDATE` closes that: the second transaction blocks here until the
        first commits, and then reads the values the first one wrote. A row
        lock rather than an advisory lock, unlike the cycle guards in 010 and
        022, because the row that has to be locked is known before the read
        starts -- there is no walk whose next step decides what to lock.

        Only meaningful inside a transaction, which is where the service calls
        it; outside one the lock is released the instant this statement returns
        and the window is exactly as wide as it was.

        Returns None for a document in another workspace, which is the same
        answer `get_by_id` gives and for the same reason. No row is locked in
        that case, and none needs to be.
        """
        row = await connection.fetchrow(
            f"""
            SELECT {_DOCUMENT_COLUMNS}
            FROM documents
            WHERE workspace_id = $1 AND id = $2
            FOR UPDATE
            """,
            scope.workspace_id,
            document_id,
        )

        if row is None:
            return None

        return self._to_document(row)

    async def content_matches(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        content: str,
    ) -> bool:
        """Whether the document already holds exactly this content.

        The comparison is made by PostgreSQL and not by this process, and the
        reason is both correctness and size. Correctness: `=` on jsonb is
        SEMANTIC -- key order, duplicate keys and whitespace do not count --
        which is the comparison "has anything changed" actually means, and
        which a string comparison of two renderings would only approximate.
        Size: the stored tree may be 200,000 characters, and pulling it into
        this process to answer a boolean would put the whole document on the
        wire on every autosave.

        A document in another workspace answers False rather than raising:
        `fetchval` returns None for no row, and False is the safe reading --
        "not known to be unchanged" makes the caller attempt the write, which
        the workspace predicate on the UPDATE then refuses on its own terms.
        """
        matches = await connection.fetchval(
            """
            SELECT content = $3::JSONB
            FROM documents
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            document_id,
            content,
        )

        return matches is True

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        set_title: bool,
        title: str | None,
        set_content: bool,
        content: str | None,
        editor_id: UUID,
    ) -> DocumentEntity | None:
        """Apply a partial edit, or return nothing if there is no such row.

        Each field arrives as a pair -- a flag saying whether the caller
        mentioned it, and the value -- for the reason ProjectRepository.update
        gives: `COALESCE($n, column)` cannot distinguish a NULL the caller
        asked for from a field it never mentioned. Neither column is nullable
        here, so the flag is not strictly needed to express a clear; it is kept
        because one static statement with explicit casts is what every other
        update in this codebase looks like, and a second shape would be a
        second thing to read.

        `content` arrives already rendered, as the JSON text
        `render_content` produced. The parse happened in the service, over the
        value the client sent, and re-rendering it here would be a second
        encoder to keep byte-compatible with the first.

        `last_edited_by` is stamped unconditionally, and it is the editor's own
        id -- read off the authorized scope by the resolver, never sent by a
        client.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE documents
            SET
                title = CASE WHEN $3::BOOLEAN THEN $4::TEXT ELSE title END,
                content = CASE
                    WHEN $5::BOOLEAN THEN $6::JSONB ELSE content
                END,
                last_edited_by = $7,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING {_DOCUMENT_COLUMNS}
            """,
            scope.workspace_id,
            document_id,
            set_title,
            title,
            set_content,
            content,
            editor_id,
        )

        if row is None:
            return None

        return self._to_document(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
    ) -> bool:
        """Delete the document, reporting whether there was one to delete.

        The workspace is in the predicate, so this cannot reach another
        tenant's document however the id was obtained.

        This does NOT remove the rows that reference it: both foreign keys onto
        `documents` are ON DELETE RESTRICT, so a document still carrying
        revisions or comments makes the server refuse this statement.
        `DocumentService.delete` clears them first, in one transaction.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM documents
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            document_id,
        )

        return status == "DELETE 1"

    # ------------------------------------------------------------- revisions

    async def create_revision(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
    ) -> DocumentRevisionEntity | None:
        """Copy the document's CURRENT title, content and author into history.

        One statement, `INSERT ... SELECT`, and that shape is the point: the
        version being preserved is read and written inside the same statement,
        so there is no moment at which this process holds a copy that the row
        could have moved on from. It also means the potentially large content
        never leaves the server -- a snapshot costs no bytes on the wire.

        `last_edited_by` becomes the revision's `author_id`, because the person
        being recorded is whoever WROTE this version, not whoever is replacing
        it. See the note on that column in migrations/023_documents.sql.

        The workspace is in the SELECT's predicate, so a document in another
        tenant selects no row, inserts no row, and returns None -- rather than
        snapshotting somebody else's writing into this workspace's history.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO document_revisions (
                workspace_id, document_id, title, content, author_id
            )
            SELECT
                documents.workspace_id,
                documents.id,
                documents.title,
                documents.content,
                documents.last_edited_by
            FROM documents
            WHERE documents.workspace_id = $1 AND documents.id = $2
            RETURNING {_REVISION_COLUMNS}
            """,
            scope.workspace_id,
            document_id,
        )

        if row is None:
            return None

        return self._to_revision(row)

    async def restore(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        revision_id: UUID,
        editor_id: UUID,
    ) -> DocumentEntity | None:
        """Put a past version back, in one statement, or report nothing.

        The revision is selected by workspace AND document AND id -- all three.
        A revision id from another tenant matches no row, and so does a real
        revision of a DIFFERENT document in this workspace, so neither can be
        used to write one document's history into another. The UPDATE then
        affects nothing and this returns None, which the service reports as the
        same NOT_FOUND an id that never existed gets.

        The content moves from one column to another without passing through
        `parse_content`, and that is a deliberate exception rather than an
        oversight. Everything in `document_revisions` was copied from
        `documents.content`, which was parsed when it was written; and whatever
        this writes will be parsed again on the way out, by `_to_content`, like
        every other read. So the invariant the repository actually guarantees
        -- nothing reaches a client unparsed -- holds without a round trip that
        would pull up to 200,000 characters into this process and push them
        straight back. A revision that a later, stricter parser would refuse
        makes the restored document unreadable rather than dangerous, which is
        the correct direction for that failure.

        `last_edited_by` becomes the person doing the restoring. A restore IS
        an edit -- they chose to make the document say this again -- and
        attributing it to the original author would make the history claim
        somebody wrote something at a time they did not.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE documents
            SET
                title = document_revisions.title,
                content = document_revisions.content,
                last_edited_by = $4,
                updated_at = now()
            FROM document_revisions
            WHERE documents.workspace_id = $1
                AND documents.id = $2
                AND document_revisions.workspace_id = $1
                AND document_revisions.document_id = $2
                AND document_revisions.id = $3
            RETURNING {_DOCUMENT_COLUMNS_QUALIFIED}
            """,
            scope.workspace_id,
            document_id,
            revision_id,
            editor_id,
        )

        if row is None:
            return None

        return self._to_document(row)

    async def clear_revisions(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
    ) -> None:
        """Drop one document's whole history, on the way to deleting it."""
        await connection.execute(
            """
            DELETE FROM document_revisions
            WHERE workspace_id = $1 AND document_id = $2
            """,
            scope.workspace_id,
            document_id,
        )

    # -------------------------------------------------------------- comments

    async def create_comment(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        author_id: UUID,
        body: str,
    ) -> DocumentCommentEntity:
        """Write one comment onto one document in this workspace.

        `CommentRepository.create` with a different parent, down to leaving
        `edited_at` NULL rather than seeding it with `created_at`: a comment
        written and never touched has no edit to report, and seeding the column
        would make "has this been edited" unanswerable from the row.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO document_comments (
                workspace_id, document_id, author_id, body
            )
            VALUES ($1, $2, $3, $4)
            RETURNING {_COMMENT_COLUMNS}
            """,
            scope.workspace_id,
            document_id,
            author_id,
            body,
        )

        return self._to_comment(row)

    async def delete_comment(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        comment_id: UUID,
        author_id: UUID,
    ) -> bool:
        """Remove one of this author's comments; True if a row went.

        Both the workspace and the author are equalities in the WHERE clause,
        never a check applied to a row already read. A comment in another
        tenant, a comment by another author, and an id that exists nowhere all
        delete nothing and answer False -- one answer, reached without this
        process ever loading a row it was not entitled to.
        """
        deleted = await connection.fetchval(
            """
            DELETE FROM document_comments
            WHERE workspace_id = $1 AND id = $2 AND author_id = $3
            RETURNING id
            """,
            scope.workspace_id,
            comment_id,
            author_id,
        )

        return deleted is not None

    async def list_comments(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> Comments:
        """Keyset page of one document's comments, oldest first.

        Ascending, like `CommentRepository.list_for_issue` and unlike every
        other list here: a discussion reads forwards, and a reader arriving at
        a document wants the first thing said, not the last.

        Both equalities are ANDed with the cursor rather than folded into it. A
        row-value comparison widened to include workspace_id or document_id
        would put them into the ORDER BY, which is how a page walk leaves one
        document -- or one tenant -- and continues into whichever sorts next.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                f"""
                SELECT {_COMMENT_COLUMNS}
                FROM document_comments
                WHERE workspace_id = $1 AND document_id = $2
                ORDER BY created_at, id
                LIMIT $3
                """,
                scope.workspace_id,
                document_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                f"""
                SELECT {_COMMENT_COLUMNS}
                FROM document_comments
                WHERE workspace_id = $1
                    AND document_id = $2
                    AND (created_at, id) > ($3, $4)
                ORDER BY created_at, id
                LIMIT $5
                """,
                scope.workspace_id,
                document_id,
                after_created_at,
                after_id,
                limit,
            )

        return [self._to_comment(row) for row in rows]

    async def clear_comments(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
    ) -> None:
        """Drop one document's whole discussion, on the way to deleting it."""
        await connection.execute(
            """
            DELETE FROM document_comments
            WHERE workspace_id = $1 AND document_id = $2
            """,
            scope.workspace_id,
            document_id,
        )

    # --------------------------------------------------------------- mapping

    @staticmethod
    def _to_document(row: asyncpg.Record) -> DocumentEntity:
        return DocumentEntity(
            id=row["id"],
            title=row["title"],
            project_id=row["project_id"],
            initiative_id=row["initiative_id"],
            creator_id=row["creator_id"],
            last_edited_by=row["last_edited_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_revision(row: asyncpg.Record) -> DocumentRevisionEntity:
        return DocumentRevisionEntity(
            id=row["id"],
            document_id=row["document_id"],
            title=row["title"],
            author_id=row["author_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_comment(row: asyncpg.Record) -> DocumentCommentEntity:
        return DocumentCommentEntity(
            id=row["id"],
            document_id=row["document_id"],
            author_id=row["author_id"],
            body=row["body"],
            edited_at=row["edited_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_content(stored: str) -> DocumentContent:
        """One stored tree, parsed rather than trusted.

        Called HERE rather than left to a caller, so there is no shape of this
        feature in which a raw stored document is reachable above the
        repository -- the same arrangement `SavedViewRepository._to_view` has
        with `decode_filter`, and the reason this class's docstring spends a
        section on it.

        `stored` arrives as `str` and not as a dict: asyncpg decodes JSONB to
        text unless a codec is registered, and registering one here would make
        this mapping depend on connection setup performed somewhere else.

        Every failure becomes `InvalidStoredContentError`, including a
        `JSONDecodeError`, which would mean the column is not valid JSON at all
        -- impossible while it is JSONB, and exactly the sort of impossibility
        that stops being one when someone changes a column type. `TypeError`
        covers the same possibility one step earlier: the annotation says `str`
        because that is what asyncpg produces for a JSONB column, but the value
        arrives out of a Record as `Any`, so nothing checks it before this line
        except this line.
        """
        try:
            return parse_content(json.loads(stored))
        except (DocumentContentError, TypeError, ValueError):
            raise InvalidStoredContentError() from None


def _filter_clause(
    document_filter: DocumentFilter,
    *,
    first_index: int,
) -> tuple[str, list[object]]:
    """The parent predicate, and the values it reads.

    Values NEVER reach the SQL -- the templates are literals in this module and
    the caller contributes only a bound parameter, which is the property
    `_Predicates` in app/repositories/issues.py exists to make obvious. This is
    that class at the one size it is needed here; two optional filters did not
    justify importing a builder whose whole reason for existing is eight.

    `first_index` is the parameter number this clause may start at, so the
    caller stays in charge of the numbering its own cursor and limit continue.
    """
    clauses: list[str] = []
    values: list[object] = []

    if document_filter.by_project:
        values.append(document_filter.project_id)
        clauses.append(
            f"project_id IS NOT DISTINCT FROM ${first_index + len(values) - 1}"
        )

    if document_filter.by_initiative:
        values.append(document_filter.initiative_id)
        clauses.append(
            f"initiative_id IS NOT DISTINCT FROM ${first_index + len(values) - 1}"
        )

    return "".join(f"\n                    AND {clause}" for clause in clauses), values
