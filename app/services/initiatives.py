from collections.abc import Sequence
from datetime import date
from typing import NoReturn
from uuid import UUID

import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.health import HEALTH_VALUES
from app.domain.initiatives import (
    INITIATIVE_STATUSES,
    MAX_INITIATIVE_DEPTH,
    InitiativeEntity,
    InitiativePage,
    InitiativeUpdateEntity,
)
from app.domain.pagination import (
    InvalidCursorError,
    KeysetCursor,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from app.domain.patch import UNSET, UnsetType
from app.domain.tenancy import WorkspaceScope
from app.repositories.initiatives import InitiativeRepository


# The same alias, for the same reason, as in app/services/projects.py --
# `InitiativeService` has a method called `list`, which shadows the builtin for
# every annotation below it in the class body. app/repositories/projects.py has
# the full explanation, including why `from __future__ import annotations` is
# the wrong fix.
Updates = list[InitiativeUpdateEntity]


NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 200

DESCRIPTION_MAX_LENGTH = 10_000

# Matches `initiative_updates_body_length` in migrations/022_initiatives.sql,
# and the same pairing app/services/projects.py describes: the database has to
# refuse an oversized body whoever writes it, and this layer has to refuse one
# without turning a CheckViolationError into a user-facing message.
UPDATE_BODY_MAX_LENGTH = 10_000

FIRST_MIN = 1
FIRST_MAX = 100

# The most updates one initiative answers with, and the same bound and the same
# ceiling app/services/projects.py documents on UPDATE_LIST_LIMIT.
#
# ponytail: a history cap, not pagination. The upgrade is a cursor on
# `Initiative.updates`; raising the number would paper over an unpaginated list
# instead of paginating it.
UPDATE_LIST_LIMIT = 200


# Constraint name -> the field error it means, for the violations that are
# ordinary consequences of client input rather than defects.
#
# Keyed on the constraint name and not on the exception class, because two
# constraints on one statement raise the same class and mean entirely different
# things: on `initiative_projects` a foreign key failure is either "no such
# initiative" or "no such project", and reporting the wrong one sends a client
# to correct the wrong half of its request.
#
# What is deliberately NOT distinguished is the tenant. A project from another
# workspace and a project that does not exist both break
# `initiative_projects_project_fk`, so both produce the same NOT_FOUND. Telling
# them apart would require a lookup this service does not perform, and
# performing it would answer a question -- "does this id exist somewhere I
# cannot see" -- that no client may be allowed to ask.
_CONSTRAINT_ERRORS: dict[str, ValidationIssue] = {
    "initiative_projects_initiative_fk": ValidationIssue(
        field="initiativeId",
        code="NOT_FOUND",
        message="Initiative not found",
    ),
    "initiative_projects_project_fk": ValidationIssue(
        field="projectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    "initiative_projects_pkey": ValidationIssue(
        field="projectId",
        code="ALREADY_ASSOCIATED",
        message="Project is already in this initiative",
    ),
    # The owner is not a member of this initiative's workspace.
    #
    # One message for three situations that must stay indistinguishable: no
    # such user anywhere, a real user in some other workspace, and a real
    # member whose membership was revoked between the client reading the member
    # list and sending this. Naming which would let anyone holding a workspace
    # ask "is this person a member of yours?" for any user id they can guess.
    "initiatives_owner_fk": ValidationIssue(
        field="ownerId",
        code="NOT_MEMBER",
        message="Owner must be a member of this workspace",
    ),
    # The proposed parent is not an initiative in this workspace. Reported
    # against the field the client sent rather than against the subject, for
    # the reason RelationService._not_found gives.
    "initiatives_parent_fk": ValidationIssue(
        field="parentInitiativeId",
        code="NOT_FOUND",
        message="Initiative not found",
    ),
    "initiative_updates_initiative_fk": ValidationIssue(
        field="initiativeId",
        code="NOT_FOUND",
        message="Initiative not found",
    ),
    "initiative_updates_author_fk": ValidationIssue(
        field="authorId",
        code="NOT_MEMBER",
        message="Author must be a member of this workspace",
    ),
}

_INITIATIVE_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Initiative not found",
)

# `CYCLE` is the code RelationService already publishes for the sub-issue
# version of this refusal, reused rather than invented so that a client learns
# one vocabulary for one concept.
_PARENT_CYCLE = ValidationIssue(
    field="parentInitiativeId",
    code="CYCLE",
    message="That initiative is already below this one",
)

_SELF_PARENT = ValidationIssue(
    field="parentInitiativeId",
    code="SELF_PARENT",
    message="An initiative cannot be its own parent",
)


def _raise_mapped(error: asyncpg.PostgresError) -> NoReturn:
    """Translate a named constraint violation, or re-raise it untouched.

    The re-raise is the important half. A violation this service did not
    anticipate is a defect -- a missing NOT NULL, a constraint added by a later
    migration nobody taught this mapping about -- and turning it into a field
    error would tell a client to fix its input for a problem that is not in the
    input, while hiding the defect behind a 200.
    """
    issue = _CONSTRAINT_ERRORS.get(error.constraint_name or "")

    if issue is None:
        raise error

    raise ValidationError([issue]) from None


class InitiativeService:
    """Business rules for initiatives, their projects, tree and updates.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service also
    owns connection acquisition and transaction boundaries.

    The workspace is threaded through as an argument on every method rather
    than held on the instance, for the reason IssueService states: an instance
    attribute becomes an ambient current workspace that the next operation
    inherits without asking.

    Holding a scope is not permission to act in it. Nothing in this class
    checks that the caller belongs to the workspace it named.

    ## Where tenancy is actually enforced

    Not in this file. Every cross-workspace association is refused by a
    composite foreign key in migrations/022_initiatives.sql, and this service's
    only job on that path is to turn the refusal into a message. There is no
    SELECT that reads an initiative to check its workspace before writing a row
    that references it.

    ## The one rule the database cannot hold

    `set_parent` is the exception, and it is the reason this class opens a
    transaction around what looks like a single UPDATE. A cycle among
    initiatives cannot be refused by a constraint -- no CHECK may read a second
    row -- so `set_parent` reads a hierarchy and then writes, and those two
    steps have to be one atomic act or the guarantee is decorative. See
    `InitiativeRepository.lock_parenting` for what makes them one, and for the
    honest statement of what that does not cover.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: InitiativeRepository,
    ):
        self._pool = pool
        self._repository = repository

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
    ) -> InitiativeEntity | None:
        """One initiative from this workspace, or nothing.

        "Not in this workspace" and "does not exist" are the same answer on
        purpose; the repository explains why the distinction must not be
        observable.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(
                connection,
                scope=scope,
                initiative_id=initiative_id,
            )

    async def list(
        self,
        *,
        scope: WorkspaceScope,
        first: int,
        after: str | None,
    ) -> InitiativePage:
        """Forward keyset page of one workspace's initiatives, newest first.

        A single SELECT needs no explicit write transaction, so this acquires a
        connection without opening one.

        The cursor is not trusted to carry a workspace and could not be if it
        did: it is Base64 over JSON, readable and writable by anyone holding
        it. The scope comes from this call, so a cursor minted in one workspace
        and replayed against another selects nothing rather than resuming
        someone else's page.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list(
                connection,
                scope=scope,
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

        return InitiativePage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    async def list_updates(
        self,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
    ) -> Updates:
        """One initiative's update history, newest first.

        An initiative in another workspace, and one that does not exist, both
        produce an empty list -- the same answer a real initiative nobody has
        posted about gives.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list_updates(
                connection,
                scope=scope,
                initiative_id=initiative_id,
                limit=UPDATE_LIST_LIMIT,
            )

    async def list_updates_for_initiatives(
        self,
        *,
        scope: WorkspaceScope,
        # `Sequence`, not `list`: the name means the method here, as the note
        # on `Updates` above explains. It reads better anyway -- this only
        # iterates the ids.
        initiative_ids: Sequence[UUID],
    ) -> Updates:
        """Several initiatives' updates at once, for batching."""
        if not initiative_ids:
            return []

        async with self._pool.acquire() as connection:
            return await self._repository.list_updates_for_initiatives(
                connection,
                scope=scope,
                initiative_ids=initiative_ids,
                limit_per_initiative=UPDATE_LIST_LIMIT,
            )

    # ------------------------------------------------------------ initiative

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        name: str,
        description: str | None,
        status: str,
        target_date: date | None,
        owner_id: UUID | None = None,
    ) -> InitiativeEntity:
        """Create one top-level initiative, with no projects and no parent.

        Projects are a separate call rather than a list on this one, for the
        reason ProjectService.create gives about teams: associating a project
        can fail on its own terms, and folding those failures into creation
        would mean either abandoning the initiative over one bad id or
        reporting a partial success that no payload shape describes well.

        A parent is a separate call for a different and stronger reason: it is
        the only path that takes the lock and runs the cycle and depth guard,
        and an `initiativeCreate` that accepted one would be a second writer of
        `parent_initiative_id` outside that guard -- which
        migrations/022_initiatives.sql names as exactly the way this guarantee
        is lost with no failing test to say so.

        The owner IS on this call, and the asymmetry is not an inconsistency: a
        owner is a column on the row being inserted, so it succeeds or fails
        with the insert and there is no partial state to report.
        """
        self._validate_initiative_fields(
            name=name,
            description=description,
            status=status,
        )

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block will
            # also carry the audit / sync / outbox writes.
            async with connection.transaction():
                try:
                    return await self._repository.create(
                        connection,
                        scope=scope,
                        name=name,
                        description=description,
                        status=status,
                        target_date=target_date,
                        owner_id=owner_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    # Only initiatives_owner_fk can fire here -- it is the one
                    # foreign key on this INSERT whose value came from a
                    # client. initiatives_workspace_fk breaking would mean the
                    # request resolved a workspace that no longer exists, which
                    # is not something the caller can correct, so
                    # `_raise_mapped` re-raises it rather than reporting it as
                    # bad input.
                    _raise_mapped(error)

    async def update(
        self,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        name: str | UnsetType = UNSET,
        description: str | None | UnsetType = UNSET,
        status: str | UnsetType = UNSET,
        target_date: date | None | UnsetType = UNSET,
        owner_id: UUID | None | UnsetType = UNSET,
    ) -> InitiativeEntity:
        """Apply a partial update and return the initiative as it now stands.

        `UNSET` and `None` are different arguments: UNSET leaves a field alone,
        None clears it. `name` and `status` cannot be cleared -- the columns are
        NOT NULL -- so their types admit no None at all.

        Neither the parent nor the health is patchable here. The parent is
        `set_parent`'s because it needs the lock; the health is
        `post_update`'s, because setting it without recording who said so and
        why is exactly what the update log exists to prevent.

        A patch that sets nothing does not reach the database. The UPDATE would
        be harmless except for `updated_at = now()`, and stamping a row as
        modified because a client sent an empty form is a lie that propagates
        into every "recently changed" view built on that column.
        """
        self._validate_initiative_fields(
            name=name,
            description=description,
            status=status,
        )

        if (
            name is UNSET
            and description is UNSET
            and status is UNSET
            and target_date is UNSET
            and owner_id is UNSET
        ):
            existing = await self.get_by_id(scope=scope, initiative_id=initiative_id)

            if existing is None:
                raise ValidationError([_INITIATIVE_NOT_FOUND])

            return existing

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    updated = await self._repository.update(
                        connection,
                        scope=scope,
                        initiative_id=initiative_id,
                        set_name=name is not UNSET,
                        name=None if isinstance(name, UnsetType) else name,
                        set_description=description is not UNSET,
                        description=(
                            None if isinstance(description, UnsetType) else description
                        ),
                        set_status=status is not UNSET,
                        status=None if isinstance(status, UnsetType) else status,
                        set_target_date=target_date is not UNSET,
                        target_date=(
                            None if isinstance(target_date, UnsetType) else target_date
                        ),
                        set_owner_id=owner_id is not UNSET,
                        owner_id=None if isinstance(owner_id, UnsetType) else owner_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

        if updated is None:
            raise ValidationError([_INITIATIVE_NOT_FOUND])

        return updated

    async def delete(self, *, scope: WorkspaceScope, initiative_id: UUID) -> None:
        """Delete one initiative and detach everything hanging off it.

        The detachment is written out rather than delegated to ON DELETE
        CASCADE, and the order is the order the foreign keys require:

            children -> project links -> updates -> the initiative itself

        Two reasons for doing it here. The obvious one is that CASCADE would
        make `DELETE FROM initiatives WHERE id = ...` silently rewrite rows in
        three other tables -- and, through the self-reference, delete the entire
        tree beneath it -- while reporting `DELETE 1`. The less obvious one is
        that the three are not treated alike: children are PROMOTED to the top
        level, project links are dropped, and the update history is destroyed
        with the initiative it is about. That is a product decision, which
        belongs in a service where it can be read and changed, not in a schema
        clause.

        All of it in one transaction, so a failure part way through leaves the
        initiative intact with its tree still attached rather than half
        dismantled.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.clear_children(
                    connection,
                    scope=scope,
                    initiative_id=initiative_id,
                )
                await self._repository.clear_projects(
                    connection,
                    scope=scope,
                    initiative_id=initiative_id,
                )
                await self._repository.clear_updates(
                    connection,
                    scope=scope,
                    initiative_id=initiative_id,
                )

                deleted = await self._repository.delete(
                    connection,
                    scope=scope,
                    initiative_id=initiative_id,
                )

                if not deleted:
                    # Raised inside the transaction so it rolls back. Nothing
                    # above it could have matched a row -- the initiative is not
                    # in this workspace, so neither is anything referencing it
                    # -- but relying on that to leave the database untouched
                    # would be relying on an argument rather than on the
                    # rollback that makes it true.
                    raise ValidationError([_INITIATIVE_NOT_FOUND])

    # -------------------------------------------------------- the hierarchy

    async def set_parent(
        self,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        parent_id: UUID,
    ) -> InitiativeEntity:
        """Nest one initiative under another, in the same workspace.

        Four refusals, in the order they become knowable:

        * An initiative cannot be its own parent. This is a comparison of two
          arguments -- it reads no row, so there is nothing to race, and it is
          settled before a connection is taken. `initiatives_parent_not_self`
          says the same thing in the database and remains the guarantee; this
          only produces the better message.
        * The proposed parent must not already be below this initiative. That
          is the cycle guard, and unlike the first it is enforced by this code
          rather than by a constraint. It runs inside the transaction, after
          the workspace's parenting lock, so no concurrent re-parent can
          invalidate the hierarchy it read.
        * The move must not push the tree past MAX_INITIATIVE_DEPTH. Checked
          over BOTH ends -- how deep the parent already is, and how tall the
          sub-tree being moved is -- because a three-deep sub-tree under a
          three-deep parent is too deep even though neither half is.
        * Both initiatives must exist in this workspace. Not checked here at
          all: the UPDATE returning no row means the subject is absent, and
          `initiatives_parent_fk` refusing means the parent is.
        """
        if parent_id == initiative_id:
            raise ValidationError([_SELF_PARENT])

        async with self._pool.acquire() as connection:
            # The transaction is load-bearing rather than conventional: the
            # advisory lock below is released at its end, and it must not be
            # released until the write it protects has committed.
            async with connection.transaction():
                await self._repository.lock_parenting(connection, scope=scope)

                check = await self._repository.inspect_parenting(
                    connection,
                    scope=scope,
                    parent_id=parent_id,
                    initiative_id=initiative_id,
                    max_depth=MAX_INITIATIVE_DEPTH,
                )

                if check.creates_cycle:
                    raise ValidationError([_PARENT_CYCLE])

                depth = check.resulting_depth()

                if depth > MAX_INITIATIVE_DEPTH:
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="parentInitiativeId",
                                code="TOO_DEEP",
                                message=(
                                    "Initiatives may be nested at most "
                                    f"{MAX_INITIATIVE_DEPTH} levels below a "
                                    "top-level one; this would reach "
                                    f"{depth}"
                                ),
                            )
                        ]
                    )

                try:
                    entity = await self._repository.set_parent(
                        connection,
                        scope=scope,
                        initiative_id=initiative_id,
                        parent_id=parent_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

        if entity is None:
            raise ValidationError([_INITIATIVE_NOT_FOUND])

        return entity

    async def clear_parent(
        self,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
    ) -> InitiativeEntity:
        """Move one initiative back to the top level.

        No lock, no cycle check and no depth check: removing an edge cannot
        close a loop and cannot deepen a tree. An initiative that already has
        no parent is returned unchanged rather than reported as an error --
        the request names a state, and that state already holds.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                entity = await self._repository.set_parent(
                    connection,
                    scope=scope,
                    initiative_id=initiative_id,
                    parent_id=None,
                )

        if entity is None:
            raise ValidationError([_INITIATIVE_NOT_FOUND])

        return entity

    # ---------------------------------------------------------- project links

    async def add_project(
        self,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        project_id: UUID,
    ) -> InitiativeEntity:
        """Put one project into one initiative, and return the initiative.

        This is the operation the whole feature exists for: an initiative is
        the several projects that add up to a goal, so this is called more than
        once per initiative and each call stands alone.

        The initiative and the project are never looked up first.
        `initiative_projects` holds one `workspace_id` for the row and both of
        its foreign keys read it, so PostgreSQL is what refuses a project from
        another workspace -- as one statement, with nothing in between for a
        concurrent move to exploit.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    await self._repository.add_project(
                        connection,
                        scope=scope,
                        initiative_id=initiative_id,
                        project_id=project_id,
                    )
                except (
                    asyncpg.ForeignKeyViolationError,
                    asyncpg.UniqueViolationError,
                ) as error:
                    _raise_mapped(error)

                initiative = await self._repository.get_by_id(
                    connection,
                    scope=scope,
                    initiative_id=initiative_id,
                )

        if initiative is None:
            # Unreachable through the constraint above -- the INSERT succeeded,
            # so an initiative with this id exists in this workspace -- but
            # stated rather than assumed, because the alternative is returning
            # an `InitiativeEntity | None` from a method whose whole contract is
            # that it worked.
            raise ValidationError([_INITIATIVE_NOT_FOUND])

        return initiative

    async def remove_project(
        self,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        project_id: UUID,
    ) -> InitiativeEntity:
        """Take one project out of one initiative, and return the initiative.

        A project that was not in the initiative, an initiative in another
        workspace and one that never existed are all answered the same way: the
        delete matches nothing, and the reload below decides between "here is
        the initiative" and NOT_FOUND. Removal is not reported as a failure
        when there was nothing to remove, because the caller's intent already
        holds.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.remove_project(
                    connection,
                    scope=scope,
                    initiative_id=initiative_id,
                    project_id=project_id,
                )

                initiative = await self._repository.get_by_id(
                    connection,
                    scope=scope,
                    initiative_id=initiative_id,
                )

        if initiative is None:
            raise ValidationError([_INITIATIVE_NOT_FOUND])

        return initiative

    # --------------------------------------------------------------- updates

    async def post_update(
        self,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        health: str,
        body: str,
        author_id: UUID,
    ) -> InitiativeUpdateEntity:
        """Record how an initiative is going, and stamp it with the health.

        TWO writes in ONE transaction, and that is the whole method -- the same
        shape ProjectService.post_update has, and its docstring carries the
        argument in full: the row in `initiative_updates` is the history,
        `initiatives.health` is the current value, and a failure between the
        two must roll both back rather than leave an initiative claiming a
        health nothing in its history reports.

        The insert runs FIRST because it is the statement carrying the foreign
        keys: an initiative from another workspace and an author who is not a
        member are both refused there, before anything has stamped a row.

        `author_id` is not taken from the client. It arrives from the resolver
        as the session's user, and `initiative_updates_author_fk` refuses an id
        that is not a member here whatever this code passes.
        """
        self._validate_update_fields(health=health, body=body)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    entity = await self._repository.create_update(
                        connection,
                        scope=scope,
                        initiative_id=initiative_id,
                        health=health,
                        body=body,
                        author_id=author_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

                await self._repository.set_health(
                    connection,
                    scope=scope,
                    initiative_id=initiative_id,
                    health=health,
                )

        return entity

    # ------------------------------------------------------------ validation

    @staticmethod
    def _validate_list(*, first: int, after: str | None) -> KeysetCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an expected
        input error rather than a parser exception. The rules and the codes are
        IssueService's and ProjectService's, deliberately: three list endpoints
        that disagreed about the legal page size would be a contract a client
        has to learn three times.
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

    @staticmethod
    def _validate_initiative_fields(
        *,
        name: str | UnsetType,
        description: str | None | UnsetType,
        status: str | UnsetType,
    ) -> None:
        """Collect every violation, then raise once.

        Shared by create and update, which is what keeps an initiative that
        could be created from being one that could not be updated back into the
        same values. A field the caller did not mention is not validated --
        there is nothing to validate -- so UNSET short-circuits each check
        rather than being coerced into some stand-in value.

        Field order is deterministic (name, description, status) so that
        clients can rely on it. The codes and messages are a public contract.
        """
        issues: list[ValidationIssue] = []

        # Validated as supplied -- never trimmed or rewritten. A name of three
        # spaces is a name the caller typed, and silently turning it into a
        # REQUIRED failure would report an error about input the client never
        # sent.
        if not isinstance(name, UnsetType):
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

        if (
            not isinstance(description, UnsetType)
            and description is not None
            and len(description) > DESCRIPTION_MAX_LENGTH
        ):
            issues.append(
                ValidationIssue(
                    field="description",
                    code="TOO_LONG",
                    message=(
                        f"Description must be at most {DESCRIPTION_MAX_LENGTH} "
                        "characters"
                    ),
                )
            )

        if not isinstance(status, UnsetType) and status not in INITIATIVE_STATUSES:
            # The legal values are named in the message.
            # `initiatives_status_check` would reject this too, but as a
            # CheckViolationError carrying the rendered constraint -- which is
            # either masked (telling the client nothing) or forwarded (telling
            # it about the schema). Neither is a usable answer to "which
            # statuses may I send".
            issues.append(
                ValidationIssue(
                    field="status",
                    code="INVALID",
                    message=(
                        f"Status must be one of: {', '.join(INITIATIVE_STATUSES)}"
                    ),
                )
            )

        if issues:
            raise ValidationError(issues)

    @staticmethod
    def _validate_update_fields(*, health: str, body: str) -> None:
        issues: list[ValidationIssue] = []

        if health not in HEALTH_VALUES:
            issues.append(
                ValidationIssue(
                    field="health",
                    code="INVALID",
                    message=f"Health must be one of: {', '.join(HEALTH_VALUES)}",
                )
            )

        if len(body) < 1:
            issues.append(
                ValidationIssue(
                    field="body",
                    code="REQUIRED",
                    message="Body is required",
                )
            )
        elif len(body) > UPDATE_BODY_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="body",
                    code="TOO_LONG",
                    message=(
                        f"Body must be at most {UPDATE_BODY_MAX_LENGTH} characters"
                    ),
                )
            )

        if issues:
            raise ValidationError(issues)
