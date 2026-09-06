import re
from uuid import UUID

import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.labels import LabelEntity
from app.domain.pagination import (
    InvalidCursorError,
    LabelCursor,
    LabelPage,
    decode_label_cursor,
    encode_label_cursor,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.labels import LabelRepository


NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 50

FIRST_MIN = 1
FIRST_MAX = 100

# What a label is coloured if the caller does not choose. One named constant,
# in the layer that owns the rule -- migrations/007_labels_comments.sql
# deliberately gives the column no database default, because a default there
# outlives the migration and silently colours every insert that forgot.
DEFAULT_COLOR = "#6b7280"

# Accepted on the way in, in either case; stored folded to lowercase. The
# schema's `labels_color_format` admits lowercase only, so the folding is what
# keeps '#FFFFFF' -- which is what a colour picker sends -- from arriving at
# the database as a constraint violation with nothing useful in it.
#
# Rewriting client input is done here and nowhere else, and only because a
# colour is a machine value with a canonical spelling. A label's NAME is never
# rewritten, not even trimmed: it is displayed back, so its spelling is the
# author's. `IssueService` treats a title the same way.
COLOR_PATTERN = re.compile(r"\A#[0-9a-fA-F]{6}\Z")

# How many labels one issue may wear.
#
# This exists because `Issue.labels` is an unpaginated list, which the GraphQL
# complexity rule charges as a single field (app/graphql/limits.py: a field
# declaring no `first` or `last` costs 1). Without a ceiling somewhere, one
# workspace could attach ten thousand labels to an issue and buy an unbounded
# read for a document that measured as cheap. A cap at attach time is where
# that ceiling costs nothing to enforce; capping the READ instead would
# silently truncate an issue's labels, which is worse than refusing the
# attach that went too far.
#
# It is a product limit, not a security boundary, and it is enforced without
# locking the issue: two attaches racing can both observe the count one below
# the cap and both succeed, so the true ceiling is this number plus however
# many attaches are in flight. Making it exact would mean serialising every
# attach on the issue row, which is a real cost for a limit whose only job is
# to stop the list from being unbounded.
LABELS_PER_ISSUE_MAX = 50

# The constraint names this service is prepared to see. Matching on the name
# rather than on the error class is what keeps "a duplicate label name" from
# being confused with any other unique violation the statement could ever
# raise -- a new index added later would otherwise silently start reporting
# itself as a duplicate name.
_NAME_TAKEN_CONSTRAINT = "labels_workspace_name_key"
_ALREADY_ATTACHED_CONSTRAINT = "issue_labels_pkey"
_UNKNOWN_ISSUE_CONSTRAINT = "issue_labels_issue_fk"
_UNKNOWN_LABEL_CONSTRAINT = "issue_labels_label_fk"


class LabelService:
    """Business rules for labels and for applying them to issues.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service also
    owns connection acquisition and transaction boundaries.

    The workspace is threaded through as an argument on every method rather
    than held on the instance; see `IssueService` and `WorkspaceScope` for why
    tenant identity has to travel with the operation. Holding a scope is not
    permission to act in it -- nothing in this class checks that the caller
    belongs to the workspace it named, because that check does not exist yet.

    Two kinds of failure are reported as `ValidationError`:

      * input the client can correct -- a name that is empty or too long, a
        colour that is not #rrggbb, a name another label already has;
      * an id that names nothing IN THIS WORKSPACE.

    The second is deliberately in the same channel as the first. An id is
    input, and "no such label here" is the answer a client has to act on. It
    is also the ONLY answer: a label belonging to another workspace and a
    label that never existed are indistinguishable, because telling them
    apart would let anyone holding a guessed id learn that it is real.

    Everything else -- a broken connection, a constraint this service does not
    recognise, a bug -- is left to propagate. `except ValidationError` in a
    resolver must never be the thing that catches an outage.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: LabelRepository,
        issue_label_repository: IssueLabelRepository,
    ):
        self._pool = pool
        self._repository = repository
        self._issue_labels = issue_label_repository

    async def get_by_id(
        self,
        *,
        scope: WorkspaceScope,
        label_id: UUID,
    ) -> LabelEntity | None:
        """One label from this workspace, or nothing.

        Returns None rather than raising, because this backs a nullable
        query field where "no such label" is an ordinary answer and not
        something the client got wrong.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(
                connection,
                scope=scope,
                label_id=label_id,
            )

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        name: str,
        color: str | None,
    ) -> LabelEntity:
        """Create one label in this workspace.

        `color` is optional at this boundary and not at the database's: None
        means "no preference", and DEFAULT_COLOR is substituted here so that
        every row is written with a colour that was chosen by something.

        Uniqueness is not checked before the insert. A SELECT first would be
        racy -- two requests can both find the name free -- and would put the
        rule in two places; the unique index is the one place it cannot be
        raced, so the insert is issued and its violation is translated.
        """
        chosen = DEFAULT_COLOR if color is None else color

        self._validate_attributes(name=name, color=chosen)

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block will
            # also carry the audit / sync / outbox writes.
            async with connection.transaction():
                try:
                    return await self._repository.create(
                        connection,
                        scope=scope,
                        name=name,
                        color=chosen.lower(),
                    )
                except asyncpg.UniqueViolationError as exc:
                    self._reraise_unless_name_taken(exc)

                    raise ValidationError([self._name_taken()]) from None

    async def update(
        self,
        *,
        scope: WorkspaceScope,
        label_id: UUID,
        name: str,
        color: str,
    ) -> LabelEntity:
        """Replace this label's name and colour.

        Both fields are required rather than optional, so this is a
        replacement and never a patch. A partial update would need a way to
        say "leave this alone" that is distinct from "set it to null", and
        neither field is nullable -- so the distinction would exist only to be
        explained. A client that just rendered a label already holds both
        values.
        """
        self._validate_attributes(name=name, color=color)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    entity = await self._repository.update(
                        connection,
                        scope=scope,
                        label_id=label_id,
                        name=name,
                        color=color.lower(),
                    )
                except asyncpg.UniqueViolationError as exc:
                    self._reraise_unless_name_taken(exc)

                    raise ValidationError([self._name_taken()]) from None

        if entity is None:
            raise ValidationError([self._not_found("id", "Label")])

        return entity

    async def delete(
        self,
        *,
        scope: WorkspaceScope,
        label_id: UUID,
    ) -> UUID:
        """Delete this label, returning the id that went.

        Returning the id rather than nothing so that a caller -- a client
        cache, in practice -- can evict exactly what was removed without
        having to trust the id it sent back to itself.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                deleted = await self._repository.delete(
                    connection,
                    scope=scope,
                    label_id=label_id,
                )

        if not deleted:
            raise ValidationError([self._not_found("id", "Label")])

        return label_id

    async def labels_for_issues(
        self,
        *,
        scope: WorkspaceScope,
        issue_ids: list[UUID],
    ) -> dict[UUID, list[LabelEntity]]:
        """Every listed issue's labels, in one round trip.

        Batched on behalf of `app/graphql/loaders/labels.py`. An empty batch is
        answered without touching the pool: a DataLoader never dispatches one,
        but a caller that did would otherwise pay a connection to run a query
        whose answer is known.

        Declared ABOVE `list` on purpose, and the ordering is load-bearing
        rather than aesthetic. `list` is a method name here, so from its
        definition onward the class body binds `list` to the method and the
        builtin is shadowed -- an annotation written `list[UUID]` after it
        raises `TypeError: 'function' object is not subscriptable` at import
        time, which takes the whole application down rather than one file.
        Anything annotating a builtin generic goes here; anything that does not
        may go below.
        """
        if not issue_ids:
            return {}

        async with self._pool.acquire() as connection:
            return await self._issue_labels.list_for_issues(
                connection,
                scope=scope,
                issue_ids=issue_ids,
            )

    async def list(
        self,
        *,
        scope: WorkspaceScope,
        first: int,
        after: str | None,
    ) -> LabelPage:
        """Forward keyset page of one workspace's labels, alphabetically.

        A single SELECT needs no explicit write transaction, so this acquires
        a connection without opening one.

        The cursor is not trusted to carry a workspace and could not be if it
        did: it is Base64 over JSON, readable and writable by anyone holding
        one. The scope comes from this call, so a cursor minted in one
        workspace and replayed against another resumes in the second
        workspace's own ordering rather than in the first's data.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list(
                connection,
                scope=scope,
                limit=first + 1,
                after_name=cursor.name if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        has_next_page = len(rows) > first
        nodes = rows[:first]

        end_cursor = None

        if nodes:
            # Built from the last RETURNED node, never from the extra row.
            last = nodes[-1]
            end_cursor = encode_label_cursor(last.name, last.id)

        return LabelPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    async def attach(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        label_id: UUID,
    ) -> None:
        """Apply a label to an issue, both in this workspace.

        Nothing here verifies that the issue and the label belong to the same
        tenant, and nothing here should: `issue_labels` pins both composite
        foreign keys to ONE workspace_id column, so a cross-tenant pair has no
        row that satisfies both parents and the server refuses the insert.
        This method translates that refusal into an answer; it does not
        duplicate the rule.

        The count and the insert share a transaction so that the cap is
        evaluated against a consistent view. They are not serialised against
        each other -- see LABELS_PER_ISSUE_MAX for why the limit is
        deliberately soft.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                attached = await self._issue_labels.count_for_issue(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                )

                if attached >= LABELS_PER_ISSUE_MAX:
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="labelId",
                                code="LIMIT_EXCEEDED",
                                message=(
                                    "An issue may carry at most "
                                    f"{LABELS_PER_ISSUE_MAX} labels"
                                ),
                            )
                        ]
                    )

                try:
                    await self._issue_labels.attach(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        label_id=label_id,
                    )
                except asyncpg.UniqueViolationError as exc:
                    if exc.constraint_name != _ALREADY_ATTACHED_CONSTRAINT:
                        raise

                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="labelId",
                                code="ALREADY_ATTACHED",
                                message="Label is already applied to this issue",
                            )
                        ]
                    ) from None
                except asyncpg.ForeignKeyViolationError as exc:
                    # Reported as an unknown id rather than as a tenancy
                    # error, and the wording matters: "that issue is in
                    # another workspace" would confirm that it exists.
                    if exc.constraint_name == _UNKNOWN_ISSUE_CONSTRAINT:
                        raise ValidationError(
                            [self._not_found("issueId", "Issue")]
                        ) from None

                    if exc.constraint_name == _UNKNOWN_LABEL_CONSTRAINT:
                        raise ValidationError(
                            [self._not_found("labelId", "Label")]
                        ) from None

                    raise

    async def detach(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        label_id: UUID,
    ) -> None:
        """Remove a label from an issue, both in this workspace.

        A pair that was never joined is reported rather than treated as a
        successful no-op. Silence would make "the label is gone" and "the
        label was never there, and possibly neither was the issue"
        indistinguishable to a client that is about to update its cache.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                detached = await self._issue_labels.detach(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                    label_id=label_id,
                )

        if not detached:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="labelId",
                        code="NOT_ATTACHED",
                        message="Label is not applied to this issue",
                    )
                ]
            )

    @staticmethod
    def _not_found(field: str, subject: str) -> ValidationIssue:
        """The one answer for "no such thing, here or anywhere".

        The message never distinguishes an id in another workspace from an id
        that exists nowhere, and neither does anything that produces this.
        """
        return ValidationIssue(
            field=field,
            code="NOT_FOUND",
            message=f"{subject} does not exist",
        )

    @staticmethod
    def _name_taken() -> ValidationIssue:
        return ValidationIssue(
            field="name",
            code="DUPLICATE",
            message="A label with this name already exists in this workspace",
        )

    @staticmethod
    def _reraise_unless_name_taken(error: asyncpg.UniqueViolationError) -> None:
        """Let through only the violation this service knows how to answer.

        A unique violation from any other constraint is not a duplicate name,
        whatever it has in common with one. Reporting it as one would tell a
        client to rename something that is not the problem, and would hide the
        real failure behind a 200.
        """
        if error.constraint_name != _NAME_TAKEN_CONSTRAINT:
            raise error

    @staticmethod
    def _validate_list(*, first: int, after: str | None) -> LabelCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an
        expected input error rather than a parser exception.
        """
        issues: list[ValidationIssue] = []
        cursor: LabelCursor | None = None

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
                cursor = decode_label_cursor(after)
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
    def _validate_attributes(*, name: str, color: str) -> None:
        """Collect every violation, then raise once.

        Field order is deterministic (name, then color) so that clients can
        rely on it. The codes and messages are a public contract.

        The name is validated as supplied -- never trimmed. A name of three
        spaces is one character over the minimum and is what its author typed;
        rewriting it here would mean the label that comes back is not the
        label that was asked for.
        """
        issues: list[ValidationIssue] = []

        if len(name) < NAME_MIN_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="REQUIRED",
                    message="Name is required",
                )
            )
        elif len(name) > NAME_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="TOO_LONG",
                    message=f"Name must be at most {NAME_MAX_LENGTH} characters",
                )
            )

        if COLOR_PATTERN.match(color) is None:
            issues.append(
                ValidationIssue(
                    field="color",
                    code="INVALID_FORMAT",
                    message="Color must be a hex colour such as #6b7280",
                )
            )

        if issues:
            raise ValidationError(issues)
