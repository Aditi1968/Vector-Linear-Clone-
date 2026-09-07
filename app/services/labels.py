import re
from uuid import UUID

import asyncpg

from app.domain.activity import ActivityKind
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.labels import LabelEntity, LabelGroupEntity
from app.domain.pagination import (
    InvalidCursorError,
    LabelCursor,
    LabelPage,
    decode_label_cursor,
    encode_label_cursor,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.label_groups import LabelGroupRepository
from app.repositories.labels import LabelRepository
from app.services import activity


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

# Whether a group is exclusive when the caller does not say.
#
# FALSE, and the asymmetry is deliberate: a plain group only nests labels,
# while an exclusive one refuses writes, so the safe default is the one that
# takes nothing away. Migration 021 gives the column no database default for
# the reason DEFAULT_COLOR is not one either -- a default there outlives the
# migration and silently decides for every insert that forgot.
DEFAULT_GROUP_EXCLUSIVE = False

# How many groups one workspace's group list returns.
#
# The list is not paginated: a group is an AXIS a team classifies work along
# ("Status", "Area", "Customer"), so the list is the size of a picker rather
# than of a data set. The bound is here anyway, because "a handful" is an
# expectation and not a constraint -- without it this would be an unpaginated
# field the GraphQL complexity rule charges as one, which is the same hole
# LABELS_PER_ISSUE_MAX exists to close for `Issue.labels`.
GROUPS_PER_WORKSPACE_MAX = 100

# The constraint names this service is prepared to see. Matching on the name
# rather than on the error class is what keeps "a duplicate label name" from
# being confused with any other unique violation the statement could ever
# raise -- a new index added later would otherwise silently start reporting
# itself as a duplicate name.
_NAME_TAKEN_CONSTRAINT = "labels_workspace_name_key"
_GROUP_NAME_TAKEN_CONSTRAINT = "label_groups_workspace_name_key"
_ALREADY_ATTACHED_CONSTRAINT = "issue_labels_pkey"
_EXCLUSIVE_GROUP_CONSTRAINT = "issue_labels_exclusive_group_key"
_UNKNOWN_ISSUE_CONSTRAINT = "issue_labels_issue_fk"

# What `LabelRepository.set_group` provokes for a group id that names nothing
# in this workspace.
#
# A CHECK rather than the foreign key, which reads as the wrong constraint
# until you follow the statement: `set_group` writes `group_exclusive` from a
# subquery against the named group, an unknown id makes that subquery NULL, and
# a row with `group_id` set beside a NULL `group_exclusive` is what
# `labels_group_exclusive_paired` refuses. The foreign key never gets to look
# at it, because MATCH SIMPLE skips a composite key for any row with a NULL
# among its referencing columns -- which is precisely the hole that CHECK
# exists to close.
_UNKNOWN_GROUP_CONSTRAINT = "labels_group_exclusive_paired"

# What refuses a group being made exclusive while an issue already wears two
# of its labels. Raised two tables away from the statement that provoked it,
# through migration 021's cascade, and it is expected input rather than a
# defect: a workspace asking for a rule its own data breaks is an ordinary
# thing to ask by mistake.
_GROUP_IN_USE_CONSTRAINT = "issue_labels_exclusive_group_key"


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
        group_repository: LabelGroupRepository,
    ):
        self._pool = pool
        self._repository = repository
        self._issue_labels = issue_label_repository

        # One service over all three tables, because they are one feature and
        # two of the operations span them. Deleting a group ungroups its
        # labels first, in the same transaction, and applying a label to an
        # issue is a rule about the label's group -- so a separate
        # LabelGroupService would be a second transaction boundary across a
        # single act, which is exactly what the layering rule exists to
        # prevent.
        #
        # Required, not defaulted, for the reason `IssueService` gives about
        # its team service: a default would let a context be built whose group
        # methods fail on first use rather than at construction, and every test
        # that never touches a group would go on passing.
        self._groups = group_repository

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

    # --- label groups --------------------------------------------------
    #
    # Declared ABOVE `list`, and the placement is load-bearing rather than
    # thematic, for the reason `labels_for_issues` states: `list` is a method
    # name here, so from its definition onward the class body binds `list` to
    # the method and the builtin is shadowed. An annotation written
    # `list[LabelGroupEntity]` after it raises `TypeError: 'function' object is
    # not subscriptable` at import time, which takes the whole application down
    # rather than one file. Anything annotating a builtin generic goes here.

    async def list_groups(
        self,
        *,
        scope: WorkspaceScope,
    ) -> list[LabelGroupEntity]:
        """Every label group in this workspace, alphabetically.

        Unpaginated, bounded by GROUPS_PER_WORKSPACE_MAX. See that constant for
        why a cursor would be machinery for a list that fits on a screen, and
        why the bound is here anyway.

        A single SELECT needs no explicit write transaction, so this acquires a
        connection without opening one.
        """
        async with self._pool.acquire() as connection:
            return await self._groups.list(
                connection,
                scope=scope,
                limit=GROUPS_PER_WORKSPACE_MAX,
            )

    async def get_group(
        self,
        *,
        scope: WorkspaceScope,
        group_id: UUID,
    ) -> LabelGroupEntity | None:
        """One group from this workspace, or nothing.

        Returns None rather than raising, because this backs a nullable query
        field where "no such group" is an ordinary answer and not something the
        client got wrong.
        """
        async with self._pool.acquire() as connection:
            return await self._groups.get_by_id(
                connection,
                scope=scope,
                group_id=group_id,
            )

    async def create_group(
        self,
        *,
        scope: WorkspaceScope,
        name: str,
        exclusive: bool | None,
    ) -> LabelGroupEntity:
        """Create one label group in this workspace.

        `exclusive` is optional at this boundary and not at the database's:
        None means "no preference", and DEFAULT_GROUP_EXCLUSIVE is substituted
        here so that every row is written with a value something chose.

        Uniqueness is not checked before the insert, for the reason `create`
        gives about label names: a SELECT first would be racy -- two requests
        can both find the name free -- and would put the rule in two places.
        """
        chosen = DEFAULT_GROUP_EXCLUSIVE if exclusive is None else exclusive

        self._validate_group_name(name)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    return await self._groups.create(
                        connection,
                        scope=scope,
                        name=name,
                        exclusive=chosen,
                    )
                except asyncpg.UniqueViolationError as exc:
                    if exc.constraint_name != _GROUP_NAME_TAKEN_CONSTRAINT:
                        raise

                    raise ValidationError([self._group_name_taken()]) from None

    async def update_group(
        self,
        *,
        scope: WorkspaceScope,
        group_id: UUID,
        name: str,
        exclusive: bool,
    ) -> LabelGroupEntity:
        """Replace this group's name and exclusivity.

        Both fields are required, so this is a replacement and never a patch,
        for the reason `update` gives about labels: a client that just rendered
        a group already holds both values, and a partial update would need
        "leave this alone" to be distinguishable from "set this to null" for
        two fields where null has no meaning.

        Turning exclusivity ON is the interesting case and nothing here checks
        it. Migration 021 propagates the flip down to `issue_labels` through two
        ON UPDATE CASCADE clauses, so an issue already wearing two of this
        group's labels makes the statement violate
        `issue_labels_exclusive_group_key` and the whole update fails. That is
        the rule enforced where it cannot be raced; a `HAVING count(*) > 1`
        here would be the same rule stated a second time, weaker, with a window
        in the middle during which another request can attach the label that
        breaks it.
        """
        self._validate_group_name(name)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    entity = await self._groups.update(
                        connection,
                        scope=scope,
                        group_id=group_id,
                        name=name,
                        exclusive=exclusive,
                    )
                except asyncpg.UniqueViolationError as exc:
                    if exc.constraint_name == _GROUP_NAME_TAKEN_CONSTRAINT:
                        raise ValidationError([self._group_name_taken()]) from None

                    if exc.constraint_name == _GROUP_IN_USE_CONSTRAINT:
                        raise ValidationError(
                            [
                                ValidationIssue(
                                    field="exclusive",
                                    code="GROUP_IN_USE",
                                    message=(
                                        "Some issues already carry more than "
                                        "one label from this group"
                                    ),
                                )
                            ]
                        ) from None

                    raise

        if entity is None:
            raise ValidationError([self._not_found("id", "Label group")])

        return entity

    async def delete_group(
        self,
        *,
        scope: WorkspaceScope,
        group_id: UUID,
    ) -> UUID:
        """Delete this group, returning the id that went.

        Its labels survive and keep every issue they are on; only the grouping
        goes. `labels_group_fk` is ON DELETE RESTRICT, so the two statements
        have to be in this order and in one transaction -- which is what makes
        the constraint a guard on that ordering rather than an obstacle to it.
        Migration 021 declines to choose between ungrouping and deleting the
        labels, and this is the choice made where a reader can see it.

        Returning the id rather than nothing so that a client cache can evict
        exactly what was removed without having to trust the id it sent.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._groups.ungroup_labels(
                    connection,
                    scope=scope,
                    group_id=group_id,
                )

                deleted = await self._groups.delete(
                    connection,
                    scope=scope,
                    group_id=group_id,
                )

        if not deleted:
            raise ValidationError([self._not_found("id", "Label group")])

        return group_id

    async def set_label_group(
        self,
        *,
        scope: WorkspaceScope,
        label_id: UUID,
        group_id: UUID | None,
    ) -> LabelEntity:
        """Move a label into a group, or take it out of the one it is in.

        None is the removal. There is no separate "ungroup" operation because
        there is no separate state: a label's group is one nullable value.

        Two expected failures, both reported as structured field errors:

        * the label is not this workspace's, or is not there at all -- the
          UPDATE matches nothing and there is no second statement to ask why;
        * the group is not this workspace's, or is not there at all. See
          _UNKNOWN_GROUP_CONSTRAINT for why that arrives as a CHECK violation
          rather than as the foreign key it looks like it should be, and
          `LabelRepository.set_group` for the statement that makes it one.

        Moving a label INTO an exclusive group can also be refused, by the
        cascade migration 021 declares: if any issue already wears this label
        and another from that group, the recomputed keys collide. Same code as
        the group-level flip, because it is the same refusal reached from the
        other side.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    entity = await self._repository.set_group(
                        connection,
                        scope=scope,
                        label_id=label_id,
                        group_id=group_id,
                    )
                except asyncpg.CheckViolationError as exc:
                    if exc.constraint_name != _UNKNOWN_GROUP_CONSTRAINT:
                        raise

                    raise ValidationError(
                        [self._not_found("groupId", "Label group")]
                    ) from None
                except asyncpg.UniqueViolationError as exc:
                    if exc.constraint_name != _GROUP_IN_USE_CONSTRAINT:
                        raise

                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="groupId",
                                code="GROUP_IN_USE",
                                message=(
                                    "Some issues already carry another label "
                                    "from this group"
                                ),
                            )
                        ]
                    ) from None

        if entity is None:
            raise ValidationError([self._not_found("id", "Label")])

        return entity

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
        actor_id: UUID | None = None,
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
        deliberately soft. The history row joins them in that transaction, so
        a rolled-back attach leaves no record of an attach.

        Exclusivity is NOT counted first, and the difference from the cap above
        is the point. The cap is a product limit that may be raced, so a
        count-then-insert is an honest implementation of it. Exclusivity is a
        rule: two attaches racing must not both land, and a count would let
        them, so migration 021 makes it a partial unique index and this method
        translates the refusal. The two limits in one method are implemented
        differently because they are different kinds of promise.
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
                    attached = await self._issue_labels.attach(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        label_id=label_id,
                    )
                except asyncpg.UniqueViolationError as exc:
                    if exc.constraint_name == _ALREADY_ATTACHED_CONSTRAINT:
                        raise ValidationError(
                            [
                                ValidationIssue(
                                    field="labelId",
                                    code="ALREADY_ATTACHED",
                                    message="Label is already applied to this issue",
                                )
                            ]
                        ) from None

                    if exc.constraint_name == _EXCLUSIVE_GROUP_CONSTRAINT:
                        # The group is deliberately not named in the message.
                        # It is a group of THIS workspace and the caller could
                        # look it up, so naming it leaks nothing -- but the
                        # message would then have to be built from a second
                        # read, and a client that has to act on this already
                        # knows which label it sent.
                        raise ValidationError(
                            [
                                ValidationIssue(
                                    field="labelId",
                                    code="EXCLUSIVE_GROUP",
                                    message=(
                                        "This issue already has a label from "
                                        "the same exclusive group"
                                    ),
                                )
                            ]
                        ) from None

                    raise
                except asyncpg.ForeignKeyViolationError as exc:
                    # Reported as an unknown id rather than as a tenancy
                    # error, and the wording matters: "that issue is in
                    # another workspace" would confirm that it exists.
                    if exc.constraint_name == _UNKNOWN_ISSUE_CONSTRAINT:
                        raise ValidationError(
                            [self._not_found("issueId", "Issue")]
                        ) from None

                    raise

                if not attached:
                    # No exception for an unknown label any more: migration 021
                    # made the insert read the label's `exclusivity_key` from
                    # `labels`, so an id that names nothing in this workspace
                    # selects no row and inserts none. The answer is the same
                    # one `issue_labels_label_fk` used to give and is still
                    # indistinguishable from a label belonging to another
                    # tenant.
                    raise ValidationError([self._not_found("labelId", "Label")])

                # The label's id, not its name. A name is editable and
                # deletable; the id is what the timeline can still resolve --
                # or honestly fail to resolve -- a year from now.
                await activity.record(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                    actor_id=actor_id,
                    kind=ActivityKind.LABEL_ATTACHED,
                    to_value=str(label_id),
                )

    async def detach(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        label_id: UUID,
        actor_id: UUID | None = None,
    ) -> None:
        """Remove a label from an issue, both in this workspace.

        A pair that was never joined is reported rather than treated as a
        successful no-op. Silence would make "the label is gone" and "the
        label was never there, and possibly neither was the issue"
        indistinguishable to a client that is about to update its cache.

        The history row is written only when a row actually went, and inside
        the same transaction: a timeline that recorded every ATTEMPT to
        remove a label would report removals that never happened.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                detached = await self._issue_labels.detach(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                    label_id=label_id,
                )

                if detached:
                    await activity.record(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        actor_id=actor_id,
                        kind=ActivityKind.LABEL_DETACHED,
                        to_value=str(label_id),
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
    def _group_name_taken() -> ValidationIssue:
        """The same code and field as `_name_taken`, on a different subject.

        A separate function rather than a parameter on that one, because the
        MESSAGE is what a client shows and "a label with this name" would be
        wrong on a form about groups. The code stays DUPLICATE: a client
        branching on it is asking "did the name collide", which is the same
        question either way.
        """
        return ValidationIssue(
            field="name",
            code="DUPLICATE",
            message="A label group with this name already exists in this workspace",
        )

    @staticmethod
    def _validate_group_name(name: str) -> None:
        """The same two numbers `_validate_attributes` checks for a label name.

        Shared bounds rather than a second pair, because a group name and a
        label name are rendered in the same picker and a limit that differed
        between them would be a rule nobody could state. The name is validated
        as supplied -- never trimmed -- for the reason that method gives: it is
        displayed back, so its spelling is the author's.
        """
        if len(name) < NAME_MIN_LENGTH:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="name",
                        code="REQUIRED",
                        message="Name is required",
                    )
                ]
            )

        if len(name) > NAME_MAX_LENGTH:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="name",
                        code="TOO_LONG",
                        message=f"Name must be at most {NAME_MAX_LENGTH} characters",
                    )
                ]
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
