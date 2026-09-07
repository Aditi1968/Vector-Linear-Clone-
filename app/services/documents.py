from collections.abc import Sequence
from datetime import datetime, timezone
from typing import NoReturn
from uuid import UUID

import asyncpg

from app.domain.documents import (
    COMMENT_BODY_MAX_LENGTH,
    COMMENT_BODY_MIN_LENGTH,
    REVISION_GAP,
    TITLE_MAX_LENGTH,
    TITLE_MIN_LENGTH,
    DocumentCommentEntity,
    DocumentCommentPage,
    DocumentContent,
    DocumentContentError,
    DocumentEntity,
    DocumentFilter,
    DocumentPage,
    DocumentRevisionPage,
    empty_content,
    parse_content,
    render_content,
)
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.pagination import (
    InvalidCursorError,
    KeysetCursor,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from app.domain.patch import UNSET, UnsetType
from app.domain.tenancy import WorkspaceScope
from app.repositories.documents import DocumentRepository


# The same alias, for the same reason, as in app/services/initiatives.py: this
# class has a method called `list`, which shadows the builtin for every
# annotation below it in the class body.
Contents = dict[UUID, DocumentContent]

FIRST_MIN = 1
FIRST_MAX = 100


# Constraint name -> the field error it means, for the violations that are
# ordinary consequences of client input rather than defects.
#
# Keyed on the constraint name and not on the exception class, because several
# constraints on one statement raise the same class and mean entirely different
# things -- reporting the wrong one sends a client to correct the wrong half of
# its request.
#
# What is deliberately NOT distinguished is the TENANT. A project from another
# workspace and a project that does not exist both break
# `documents_project_fk`, so both produce the same NOT_FOUND. Telling them apart
# would require a lookup this service does not perform, and performing it would
# answer a question -- "does this id exist somewhere I cannot see" -- that no
# client may be allowed to ask.
_CONSTRAINT_ERRORS: dict[str, ValidationIssue] = {
    "documents_project_fk": ValidationIssue(
        field="projectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    "documents_initiative_fk": ValidationIssue(
        field="initiativeId",
        code="NOT_FOUND",
        message="Initiative not found",
    ),
    # The viewer's membership was revoked between `authorized_scope` resolving
    # it and this statement running. Reported as itself rather than folded into
    # NOT_FOUND, because by the time this can fire the caller has already been
    # told the workspace exists -- so there is nothing left to conceal, and
    # "you are no longer a member here" is the only answer that describes what
    # happened.
    "documents_creator_fk": ValidationIssue(
        field="creatorId",
        code="NOT_MEMBER",
        message="Author must be a member of this workspace",
    ),
    "documents_last_editor_fk": ValidationIssue(
        field="editorId",
        code="NOT_MEMBER",
        message="Editor must be a member of this workspace",
    ),
    "document_revisions_author_fk": ValidationIssue(
        field="authorId",
        code="NOT_MEMBER",
        message="Author must be a member of this workspace",
    ),
    # Both foreign keys on `document_comments`, answered with the SAME message,
    # and the sameness is deliberate rather than a shortcut -- it is
    # `CommentService.create`'s rule, applied to the table that mirrors its
    # own. A viewer who does not belong to this workspace must not learn from a
    # distinguishable error that the document they named is real, and since
    # they may not read the document either, "no such document" is the only
    # true thing this server can say to them. Which of the two constraints the
    # planner checks first is therefore unobservable and does not have to be
    # pinned down.
    "document_comments_document_fk": ValidationIssue(
        field="documentId",
        code="NOT_FOUND",
        message="Document does not exist",
    ),
    "document_comments_author_fk": ValidationIssue(
        field="documentId",
        code="NOT_FOUND",
        message="Document does not exist",
    ),
}

_DOCUMENT_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Document not found",
)

# One message for a revision id that names nothing, one that belongs to another
# tenant, and one that is a real revision of a DIFFERENT document. All three
# are refusals the caller is not entitled to distinguish; see
# `DocumentRepository.restore` for why the statement cannot tell them apart
# either.
_REVISION_NOT_FOUND = ValidationIssue(
    field="revisionId",
    code="NOT_FOUND",
    message="Revision not found",
)

_COMMENT_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Comment does not exist",
)

_TWO_PARENTS = ValidationIssue(
    field="initiativeId",
    code="CONFLICT",
    message="A document belongs to a project or an initiative, not both",
)


def _raise_mapped(error: asyncpg.PostgresError) -> NoReturn:
    """Translate a named constraint violation, or re-raise it untouched.

    The re-raise is the important half. A violation this service did not
    anticipate is a defect -- a constraint added by a later migration nobody
    taught this mapping about -- and turning it into a field error would tell a
    client to fix its input for a problem that is not in the input, while
    hiding the defect behind a 200.
    """
    issue = _CONSTRAINT_ERRORS.get(error.constraint_name or "")

    if issue is None:
        raise error

    raise ValidationError([issue]) from None


class DocumentService:
    """Business rules for documents, their history and their discussion.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service also
    owns connection acquisition and transaction boundaries.

    The workspace is threaded through as an argument on every method rather
    than held on the instance, for the reason IssueService states: an instance
    attribute becomes an ambient current workspace that the next operation
    inherits without asking. Holding a scope is not permission to act in it --
    nothing in this class checks that the caller belongs to the workspace it
    named.

    ## Where tenancy is enforced

    Not in this file. Every cross-workspace association is refused by a
    composite foreign key in migrations/023_documents.sql, and every read puts
    the workspace in the WHERE clause. This service's job on that path is to
    turn a refusal into a message a client can act on, and to make sure the
    messages for "not yours" and "does not exist" are the same one.

    ## Where the content is made safe

    Also not exactly here. `parse_content` is called on the way IN by the two
    write paths below, so a crafted tree is a field error rather than a stored
    row; and it is called again on the way OUT by `DocumentRepository`, so a
    tree that got into the table some other way never reaches a client. This
    service owns only the translation of the first of those into a
    ValidationError -- see `_parse` below for why the second must NOT be
    translated the same way.

    ## The one rule the database cannot hold

    When to snapshot a version. That is a product rule about two columns and a
    clock, so it lives in `edit` -- and because deciding it means reading a row
    and then writing it, `edit` opens a transaction and takes the document's
    row lock. See `DocumentRepository.lock_for_edit` for exactly what that
    closes and what it does not.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: DocumentRepository,
    ):
        self._pool = pool
        self._repository = repository

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
    ) -> DocumentEntity | None:
        """One document from this workspace, or nothing.

        "Not in this workspace" and "does not exist" are the same answer on
        purpose; the repository explains why the distinction must not be
        observable.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(
                connection,
                scope=scope,
                document_id=document_id,
            )

    async def list(
        self,
        *,
        scope: WorkspaceScope,
        document_filter: DocumentFilter,
        first: int,
        after: str | None,
    ) -> DocumentPage:
        """Forward keyset page of this workspace's documents, newest first.

        A single SELECT needs no explicit write transaction, so this acquires a
        connection without opening one.

        The cursor is not trusted to carry a workspace and could not be if it
        did: it is Base64 over JSON, readable and writable by anyone holding
        it. The scope comes from this call, so a cursor minted in one workspace
        and replayed against another selects nothing rather than resuming
        someone else's page.

        A filter naming another workspace's project is not refused and is not
        reported: it narrows to nothing and the page comes back empty, exactly
        as a project with no documents does. That equivalence is the isolation
        property -- an error would tell a caller holding a guessed id that the
        project is real.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list(
                connection,
                scope=scope,
                document_filter=document_filter,
                limit=first + 1,
                after_created_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        has_next_page = len(rows) > first
        nodes = rows[:first]
        end_cursor = None

        if nodes:
            # Built from the last RETURNED node, never from the extra row.
            last = nodes[-1]
            end_cursor = encode_keyset_cursor(last.created_at, last.id)

        return DocumentPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    async def contents_for_documents(
        self,
        *,
        scope: WorkspaceScope,
        # `Sequence`, not `list`: the name means the method here. It reads
        # better anyway -- this only iterates the ids.
        document_ids: Sequence[UUID],
    ) -> Contents:
        """Several documents' bodies at once, for batching.

        Ids naming another workspace's document, or nothing at all, are simply
        absent from the result. The loader above turns that into null, which is
        the same answer both cases have to produce.
        """
        if not document_ids:
            return {}

        async with self._pool.acquire() as connection:
            return await self._repository.contents_for_documents(
                connection,
                scope=scope,
                document_ids=document_ids,
            )

    async def contents_for_revisions(
        self,
        *,
        scope: WorkspaceScope,
        revision_ids: Sequence[UUID],
    ) -> Contents:
        """Several revisions' bodies at once, for batching."""
        if not revision_ids:
            return {}

        async with self._pool.acquire() as connection:
            return await self._repository.contents_for_revisions(
                connection,
                scope=scope,
                revision_ids=revision_ids,
            )

    async def list_revisions(
        self,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        first: int,
        after: str | None,
    ) -> DocumentRevisionPage:
        """Forward keyset page of one document's history, newest first.

        A document in another workspace is not refused and is not reported: it
        returns an empty page, exactly as a document nobody has edited does.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            rows = await self._repository.list_revisions(
                connection,
                scope=scope,
                document_id=document_id,
                limit=first + 1,
                after_created_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        has_next_page = len(rows) > first
        nodes = rows[:first]
        end_cursor = None

        if nodes:
            last = nodes[-1]
            end_cursor = encode_keyset_cursor(last.created_at, last.id)

        return DocumentRevisionPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    async def list_comments(
        self,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        first: int,
        after: str | None,
    ) -> DocumentCommentPage:
        """Forward keyset page of one document's comments, oldest first."""
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            rows = await self._repository.list_comments(
                connection,
                scope=scope,
                document_id=document_id,
                limit=first + 1,
                after_created_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        has_next_page = len(rows) > first
        nodes = rows[:first]
        end_cursor = None

        if nodes:
            last = nodes[-1]
            end_cursor = encode_keyset_cursor(last.created_at, last.id)

        return DocumentCommentPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    # ---------------------------------------------------------------- writes

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        title: str,
        content: object | UnsetType,
        project_id: UUID | None,
        initiative_id: UUID | None,
        creator_id: UUID,
    ) -> DocumentEntity:
        """Create one document, optionally under a project or an initiative.

        `content` arrives as whatever the client sent -- an arbitrary JSON
        value -- and is parsed here, before a connection is taken. UNSET means
        the client sent none, which is a new empty document and not a missing
        argument: an editor opens a blank page and the writing happens
        afterwards.

        The creator is a required argument rather than something resolved in
        here, for the reason `CommentService.create` gives about the author: a
        service that picked one would be deciding whose writing this is by a
        rule invisible at the call site. It is the authenticated viewer --
        never an id the client sent, which would be an impersonation API.

        Neither parent is checked against the workspace before the insert, and
        neither should be. Both foreign keys are composite against this row's
        single workspace_id, so the server refuses a project or an initiative
        belonging to another tenant as part of this statement. A SELECT first
        would be a second, weaker copy of both rules: weaker because it is a
        separate statement that can be raced, and weaker because it is then two
        places that have to agree.
        """
        self._validate_title(title)
        self._validate_parents(project_id=project_id, initiative_id=initiative_id)

        parsed = (
            empty_content() if isinstance(content, UnsetType) else self._parse(content)
        )

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block will
            # also carry the audit / sync / outbox writes.
            async with connection.transaction():
                try:
                    return await self._repository.create(
                        connection,
                        scope=scope,
                        title=title,
                        content=parsed,
                        project_id=project_id,
                        initiative_id=initiative_id,
                        creator_id=creator_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

    async def edit(
        self,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        editor_id: UUID,
        title: str | UnsetType = UNSET,
        content: object | UnsetType = UNSET,
        snapshot: bool = False,
    ) -> DocumentEntity:
        """Change a document, taking a version snapshot if this is a boundary.

        ## The boundary rule

        A revision is written before the change when ANY of these holds:

        * `snapshot` -- the client asked for one. An author who has just
          finished something knows it is a boundary better than any heuristic,
          and refusing to record that would make the heuristic a ceiling rather
          than a default.
        * the incoming editor is not `last_edited_by`. Handing over is a
          boundary whatever the clock says: without this, one person's
          paragraph and the next person's deletion of it coalesce into a single
          version attributed to the second, and "what did it say before they
          touched it" -- the thing a history is actually for -- is gone.
        * the document has not been touched for `REVISION_GAP`. One continuous
          session is one version; coming back after a break starts another.

        And never otherwise, which is the half that matters: an editor
        autosaving every few seconds must not write a revision per keystroke
        burst, because a history with four hundred entries for one afternoon is
        a history nobody opens.

        ## Why this holds a transaction and a row lock

        Deciding requires READING `last_edited_by` and `updated_at` and then
        WRITING both. Two concurrent edits could each read the same "last
        edited by Ana, ten minutes ago", each conclude no snapshot was needed,
        and one version would be lost with no record it existed.
        `lock_for_edit` takes `FOR UPDATE` on the row, so the second
        transaction reads what the first one wrote.

        ## No-ops

        An edit that mentions no field, and one whose title and content both
        already match, write nothing at all -- no revision and no `updated_at`
        stamp. A document marked as modified because a client re-sent what it
        already had is a lie that propagates into every "recently changed" list
        built on that column, and a history entry identical to its neighbour is
        noise in the one list this whole feature exists to keep short.
        """
        if not isinstance(title, UnsetType):
            self._validate_title(title)

        parsed = None if isinstance(content, UnsetType) else self._parse(content)
        rendered = None if parsed is None else render_content(parsed)

        async with self._pool.acquire() as connection:
            # Load-bearing rather than conventional: the row lock below is held
            # until this block ends, and it must not be released until the
            # write it protects has committed.
            async with connection.transaction():
                current = await self._repository.lock_for_edit(
                    connection,
                    scope=scope,
                    document_id=document_id,
                )

                if current is None:
                    raise ValidationError([_DOCUMENT_NOT_FOUND])

                title_changes = (
                    not isinstance(title, UnsetType) and title != current.title
                )

                # Asked of the server rather than computed here, and only when
                # the caller actually sent content. PostgreSQL compares jsonb
                # semantically -- key order and whitespace do not count -- which
                # is the comparison this rule means; doing it in Python would
                # need the stored tree pulled into this process, which is up to
                # 200,000 characters to answer a boolean.
                content_changes = rendered is not None and not (
                    await self._repository.content_matches(
                        connection,
                        scope=scope,
                        document_id=document_id,
                        content=rendered,
                    )
                )

                if not title_changes and not content_changes:
                    return current

                if self._is_boundary(current, editor_id=editor_id, snapshot=snapshot):
                    try:
                        await self._repository.create_revision(
                            connection,
                            scope=scope,
                            document_id=document_id,
                        )
                    except asyncpg.ForeignKeyViolationError as error:
                        # `document_revisions_author_fk` -- the person who wrote
                        # the version being preserved has since been removed
                        # from the workspace. Reported rather than skipped:
                        # dropping the snapshot to get the edit through would
                        # destroy their version silently.
                        _raise_mapped(error)

                try:
                    updated = await self._repository.update(
                        connection,
                        scope=scope,
                        document_id=document_id,
                        set_title=title_changes,
                        title=None if isinstance(title, UnsetType) else title,
                        set_content=content_changes,
                        content=rendered,
                        editor_id=editor_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

        if updated is None:
            # Unreachable: the row was locked a few statements ago inside this
            # same transaction, so it cannot have gone. Stated rather than
            # assumed, because the alternative is returning a
            # `DocumentEntity | None` from a method whose whole contract is that
            # it worked.
            raise ValidationError([_DOCUMENT_NOT_FOUND])

        return updated

    async def restore(
        self,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        revision_id: UUID,
        editor_id: UUID,
    ) -> DocumentEntity:
        """Put a past version back, keeping the one it replaces.

        A restore is an ordinary edit, not a rewind. The current content is
        snapshotted FIRST and unconditionally -- no boundary heuristic -- so
        restoring is itself undoable and no version is ever destroyed by one.
        Unconditionally, because a restore is an explicit act: somebody chose
        to replace what is there, which is exactly the case the `snapshot`
        argument of `edit` exists for.

        Inside one transaction, and behind the same row lock `edit` takes, so a
        restore and a concurrent edit cannot interleave into a document whose
        history is missing one of them.

        A revision id that names nothing, one from another tenant, and a real
        revision of a DIFFERENT document all produce the same NOT_FOUND -- and
        the statement that decides cannot tell them apart either, which is what
        makes the equivalence structural rather than a choice made on the way
        out.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                current = await self._repository.lock_for_edit(
                    connection,
                    scope=scope,
                    document_id=document_id,
                )

                if current is None:
                    raise ValidationError([_DOCUMENT_NOT_FOUND])

                try:
                    await self._repository.create_revision(
                        connection,
                        scope=scope,
                        document_id=document_id,
                    )

                    restored = await self._repository.restore(
                        connection,
                        scope=scope,
                        document_id=document_id,
                        revision_id=revision_id,
                        editor_id=editor_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

                if restored is None:
                    # Raised inside the transaction so the snapshot above rolls
                    # back with it. A failed restore must not leave a version in
                    # the history that nothing superseded.
                    raise ValidationError([_REVISION_NOT_FOUND])

        return restored

    async def delete(self, *, scope: WorkspaceScope, document_id: UUID) -> None:
        """Delete one document, its history and its discussion.

        The clearing is written out rather than delegated to ON DELETE CASCADE,
        and the order is the order the foreign keys require:

            comments -> revisions -> the document itself

        CASCADE would make `DELETE FROM documents WHERE id = ...` silently
        rewrite two other tables while reporting `DELETE 1`. Destroying a
        document's history is a real decision and belongs in a service where it
        can be read and changed -- see migrations/023_documents.sql, which
        makes every key RESTRICT precisely so that this method has to exist.

        All of it in one transaction, so a failure part way through leaves the
        document intact with its history attached rather than half dismantled.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.clear_comments(
                    connection,
                    scope=scope,
                    document_id=document_id,
                )
                await self._repository.clear_revisions(
                    connection,
                    scope=scope,
                    document_id=document_id,
                )

                deleted = await self._repository.delete(
                    connection,
                    scope=scope,
                    document_id=document_id,
                )

                if not deleted:
                    # Raised inside the transaction so it rolls back. Nothing
                    # above it could have matched a row -- the document is not
                    # in this workspace, so neither is anything referencing it
                    # -- but relying on that to leave the database untouched
                    # would be relying on an argument rather than on the
                    # rollback that makes it true.
                    raise ValidationError([_DOCUMENT_NOT_FOUND])

    # -------------------------------------------------------------- comments

    async def create_comment(
        self,
        *,
        scope: WorkspaceScope,
        document_id: UUID,
        author_id: UUID,
        body: str,
    ) -> DocumentCommentEntity:
        """Write one comment onto one document in this workspace.

        `CommentService.create` with a different parent, and its reasoning
        unchanged: neither the document nor the author is checked before the
        insert, because both foreign keys are composite against this row's
        single workspace_id and the server refuses the pair as part of the
        statement that would have written it.

        Unlike the issue version, this writes no activity row, no subscription
        and no notification. Those are `issue_activity`, `issue_subscribers`
        and `notifications` from 012 and 020, every one of which is keyed to an
        ISSUE -- so wiring documents into them means widening three tables, and
        widening them polymorphically is the shape 022 and 023 both refuse. It
        is a migration of its own rather than something to approximate here.
        """
        self._validate_body(body)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    return await self._repository.create_comment(
                        connection,
                        scope=scope,
                        document_id=document_id,
                        author_id=author_id,
                        body=body,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

    async def delete_comment(
        self,
        *,
        scope: WorkspaceScope,
        comment_id: UUID,
        author_id: UUID,
    ) -> UUID:
        """Delete one of the viewer's own comments, returning the id that went.

        Three situations produce one answer: the comment is in another
        workspace, the comment was written by somebody else, and the comment
        never existed. All three are "Comment does not exist", because the two
        the caller is not entitled to must not be distinguishable from the one
        that is ordinary -- an error saying "that is not yours" confirms that
        the id names a real comment somebody really wrote.

        The authorship test is a column in the WHERE clause and not a read
        followed by a comparison, for `CommentService.delete`'s reason: a
        read-then-delete would have to fetch another author's row into this
        process to discover it was not the viewer's.

        WHO MAY DELETE: the author, and today nobody else -- the narrow half of
        the rule the product will eventually want ("the author, OR a workspace
        admin") and the half that cannot be wrong. The admin half needs a role
        check that does not exist on this path yet.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                deleted = await self._repository.delete_comment(
                    connection,
                    scope=scope,
                    comment_id=comment_id,
                    author_id=author_id,
                )

        if not deleted:
            raise ValidationError([_COMMENT_NOT_FOUND])

        return comment_id

    # ------------------------------------------------------------ validation

    @staticmethod
    def _is_boundary(
        current: DocumentEntity,
        *,
        editor_id: UUID,
        snapshot: bool,
    ) -> bool:
        """Whether this edit starts a new version. See `edit` for the argument.

        ponytail: the gap is measured against this process's clock rather than
        the database's, so a host whose time has drifted from PostgreSQL's
        decides the third condition a few seconds early or late. Harmless
        against a ten-minute window; the upgrade, if a shorter gap is ever
        wanted, is to compute `now() - updated_at >= $n` in `lock_for_edit` and
        return it beside the row.
        """
        if snapshot or current.last_edited_by != editor_id:
            return True

        return datetime.now(timezone.utc) - current.updated_at >= REVISION_GAP

    @staticmethod
    def _parse(content: object) -> DocumentContent:
        """Parse content arriving from a client, as bad input rather than a bug.

        The translation happens HERE and not in the repository, and that
        asymmetry is the whole point: the same parser refusing the same tree
        means two different things depending on which way it was travelling.
        On the way in the client can fix it, so it is a field error; on the way
        out the row is already stored, so it is a defect and
        `InvalidStoredContentError` says so. Translating both the same way
        would tell a reader to correct a request that was never the problem.

        `exc.reason` is one of the fixed strings in `app.domain.documents` and
        never contains any part of the document, so this message can be
        published without echoing attacker-chosen text back through the API.
        """
        try:
            return parse_content(content)
        except DocumentContentError as exc:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="content",
                        code="INVALID",
                        message=exc.reason,
                    )
                ]
            ) from None

    @staticmethod
    def _validate_parents(
        *,
        project_id: UUID | None,
        initiative_id: UUID | None,
    ) -> None:
        """A document belongs to at most one thing.

        `documents_one_parent` refuses the row too and remains the guarantee.
        This exists so the refusal is a field error naming the rule rather than
        a CheckViolationError carrying a rendered constraint -- which is either
        masked, telling the client nothing, or forwarded, telling it about the
        schema.
        """
        if project_id is not None and initiative_id is not None:
            raise ValidationError([_TWO_PARENTS])

    @staticmethod
    def _validate_title(title: str) -> None:
        """Validated as supplied -- never trimmed or rewritten.

        A title of three spaces is a title the caller typed, and silently
        turning it into a REQUIRED failure would report an error about input
        the client never sent. The same rule `IssueService` applies to titles
        and `CommentService` to bodies.
        """
        if len(title) < TITLE_MIN_LENGTH:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="title",
                        code="REQUIRED",
                        message="Title is required",
                    )
                ]
            )

        if len(title) > TITLE_MAX_LENGTH:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="title",
                        code="TOO_LONG",
                        message=f"Title must be at most {TITLE_MAX_LENGTH} characters",
                    )
                ]
            )

    @staticmethod
    def _validate_body(body: str) -> None:
        if len(body) < COMMENT_BODY_MIN_LENGTH:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="body",
                        code="REQUIRED",
                        message="Body is required",
                    )
                ]
            )

        if len(body) > COMMENT_BODY_MAX_LENGTH:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="body",
                        code="TOO_LONG",
                        message=(
                            f"Body must be at most {COMMENT_BODY_MAX_LENGTH} characters"
                        ),
                    )
                ]
            )

    @staticmethod
    def _validate_list(*, first: int, after: str | None) -> KeysetCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an expected
        input error rather than a parser exception. The rules and the codes are
        IssueService's and ProjectService's, deliberately: list endpoints that
        disagreed about the legal page size would be a contract a client has to
        learn once per feature.
        """
        issues: list[ValidationIssue] = []
        cursor: KeysetCursor | None = None

        if first < FIRST_MIN or first > FIRST_MAX:
            issues.append(
                ValidationIssue(
                    field="first",
                    code="OUT_OF_RANGE",
                    message=f"first must be between {FIRST_MIN} and {FIRST_MAX}",
                )
            )

        if after is not None:
            try:
                cursor = decode_keyset_cursor(after)
            except InvalidCursorError:
                issues.append(
                    ValidationIssue(
                        field="after",
                        code="INVALID_CURSOR",
                        message="Cursor is invalid",
                    )
                )

        if issues:
            raise ValidationError(issues)

        return cursor
