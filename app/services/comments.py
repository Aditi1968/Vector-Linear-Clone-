from uuid import UUID

import asyncpg

from app.domain.activity import ActivityKind
from app.domain.comments import CommentEntity
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.notifications import NotificationKind
from app.domain.pagination import (
    CommentPage,
    InvalidCursorError,
    IssueCursor,
    decode_issue_cursor,
    encode_issue_cursor,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.comments import CommentRepository
from app.services import activity


BODY_MIN_LENGTH = 1
BODY_MAX_LENGTH = 16384

FIRST_MIN = 1
FIRST_MAX = 100

# The two foreign keys on `comments`, matched by name so that any OTHER
# constraint failure stays an error instead of being reported to a client as
# something it can correct.
#
# Both are composite and both are pinned to the row's single workspace_id, so
# between them they answer every way an insert can be refused:
#
#   comments_issue_fk   -- no such issue IN THIS WORKSPACE
#   comments_author_fk  -- the viewer is not a member OF THIS WORKSPACE
#
# See `CommentService.create` for why both are answered with the same
# "Issue does not exist".
_UNKNOWN_ISSUE_CONSTRAINT = "comments_issue_fk"
_UNKNOWN_AUTHOR_CONSTRAINT = "comments_author_fk"


class CommentService:
    """Business rules for comments.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service also
    owns connection acquisition and transaction boundaries.

    The workspace is threaded through as an argument on every method rather
    than held on the instance; see `IssueService` and `WorkspaceScope`.
    Holding a scope is not permission to act in it.

    WHO MAY DELETE: the author, and today nobody else. `delete` takes the
    acting user and matches it in the WHERE clause, so a comment someone else
    wrote answers exactly as one that does not exist. That is the narrow half
    of the rule the product will eventually want -- "the author, OR a workspace
    admin" -- and the half that cannot be wrong. The admin half needs a role
    check that does not exist on this path yet, and adding it as a
    `role in (...)` comparison here would put an authorization decision in a
    service that has never been handed a role.

    The pagination cursor is the (created_at, id) codec `IssueService` uses.
    Its functions are named for issues by history and carry nothing
    issue-specific -- the payload is a timestamp and a uuid -- so comments
    share the codec rather than growing a second wire format that would have
    to be kept in step with the first.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: CommentRepository,
    ):
        self._pool = pool
        self._repository = repository

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        author_id: UUID,
        body: str,
    ) -> CommentEntity:
        """Write one comment onto one issue in this workspace.

        The author is a required argument rather than something resolved in
        here, for the reason `IssueService.create` gives about the team: a
        service that picked an author would be deciding whose words these are
        by a rule invisible at the call site. It is the authenticated viewer --
        never an id the client sent, which would be an impersonation API.

        Neither the issue nor the author is checked against the workspace
        before the insert, and neither should be. Both foreign keys are
        composite against this row's single workspace_id, so the server refuses
        an issue belonging to another tenant, and an author who is not a member
        of this one, as part of this statement. A SELECT first would be a
        second, weaker copy of both rules: weaker because it is a separate
        statement that can be raced, and weaker because it is then two places
        that have to agree.

        Both refusals are answered with the SAME "Issue does not exist", and
        the sameness is the point rather than a shortcut. A viewer who does not
        belong to this workspace must not learn from a distinguishable error
        that the issue they named is real -- and since they may not read the
        issue either, "no such issue" is the only true thing this server can
        say to them. Which of the two constraints the planner checks first is
        therefore unobservable, and does not have to be pinned down.
        """
        self._validate_body(body)

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary, and it now carries
            # three writes: the comment, the history row saying somebody
            # commented, and an inbox item for everyone but the author. A
            # comment that exists with nobody notified, or a notification
            # pointing at a comment that was rolled back, are both states
            # this block makes unreachable.
            async with connection.transaction():
                try:
                    entity = await self._repository.create(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        author_id=author_id,
                        body=body,
                    )

                    # The comment's id, not its body. A history row is a
                    # pointer to what happened, and copying the text here
                    # would make a deleted comment readable from the
                    # timeline -- which is the withdrawal not working.
                    await activity.record(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        actor_id=author_id,
                        kind=ActivityKind.COMMENTED,
                        to_value=str(entity.id),
                    )

                    # The assignee AND the issue's author: the two people a
                    # comment on this issue is addressed to even when it
                    # names nobody. The author is included here and nowhere
                    # else, which is what `include_creator` marks.
                    await activity.notify(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        actor_id=author_id,
                        kind=NotificationKind.COMMENTED,
                        include_creator=True,
                    )

                    return entity
                except asyncpg.ForeignKeyViolationError as exc:
                    if exc.constraint_name not in (
                        _UNKNOWN_ISSUE_CONSTRAINT,
                        _UNKNOWN_AUTHOR_CONSTRAINT,
                    ):
                        raise

                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="issueId",
                                code="NOT_FOUND",
                                message="Issue does not exist",
                            )
                        ]
                    ) from None

    async def delete(
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
        followed by a comparison. A read-then-delete would have to fetch
        another author's row into this process to discover it was not the
        viewer's, and would leave a window in which the row could change
        between the two statements.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                deleted = await self._repository.delete(
                    connection,
                    scope=scope,
                    comment_id=comment_id,
                    author_id=author_id,
                )

        if not deleted:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="id",
                        code="NOT_FOUND",
                        message="Comment does not exist",
                    )
                ]
            )

        return comment_id

    async def list_for_issue(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        first: int,
        after: str | None,
    ) -> CommentPage:
        """Forward keyset page of one issue's comments, oldest first.

        A single SELECT needs no explicit write transaction, so this acquires
        a connection without opening one.

        An issue in another workspace is not refused, and is not reported: it
        returns an empty page, exactly as an issue with no comments does. That
        equivalence is the isolation property -- an error, or any other
        distinguishable answer, would tell a caller holding a guessed id that
        the issue is real and simply not theirs.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list_for_issue(
                connection,
                scope=scope,
                issue_id=issue_id,
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
            end_cursor = encode_issue_cursor(last.created_at, last.id)

        return CommentPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    @staticmethod
    def _validate_list(*, first: int, after: str | None) -> IssueCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an
        expected input error rather than a parser exception.
        """
        issues: list[ValidationIssue] = []
        cursor: IssueCursor | None = None

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
                cursor = decode_issue_cursor(after)
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

    @staticmethod
    def _validate_body(body: str) -> None:
        """The body is validated as supplied -- never trimmed or rewritten.

        A body of whitespace is therefore accepted, as a title of whitespace
        is by `IssueService`. Deciding that whitespace is not content is a
        product rule nobody has made, and making it silently here would mean
        the comment that comes back is not the comment that was written.
        """
        if len(body) < BODY_MIN_LENGTH:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="body",
                        code="REQUIRED",
                        message="Body is required",
                    )
                ]
            )

        if len(body) > BODY_MAX_LENGTH:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="body",
                        code="TOO_LONG",
                        message=f"Body must be at most {BODY_MAX_LENGTH} characters",
                    )
                ]
            )
