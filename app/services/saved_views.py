from typing import NoReturn
from uuid import UUID

import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.issues import IssueFilter, IssueOrder
from app.domain.pagination import (
    InvalidCursorError,
    NameCursor,
    decode_name_cursor,
    encode_name_cursor,
)
from app.domain.patch import UNSET, UnsetType
from app.domain.saved_views import (
    SAVED_VIEW_GROUPINGS,
    SAVED_VIEW_LAYOUTS,
    SAVED_VIEW_VISIBILITIES,
    FavoriteEntity,
    SavedViewEntity,
    SavedViewPage,
)
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.repositories.saved_views import FavoriteRepository, SavedViewRepository
from app.services.issues import filter_issues


NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 200

FIRST_MIN = 1
FIRST_MAX = 100

POSITION_MIN = 0

# The most favourites one person's sidebar answers with.
#
# Not a page size and not a clamp on a user-supplied argument: no caller
# chooses it, and `favorites` takes none. It is the bound that keeps an
# unpaginated list from being unbounded, and it is set far above any real
# sidebar on purpose -- exactly as MILESTONE_LIST_LIMIT is -- so truncation is
# not a case real users reach and the honest reading is "a backstop" rather
# than "page one". When someone approaches it, this field grows a cursor the
# way `savedViews` has one.
FAVORITE_LIST_LIMIT = 500


# Constraint name -> the field error it means, for the violations that are
# ordinary consequences of client input rather than defects.
#
# Keyed on the constraint and not on the exception class, because several
# constraints on one statement raise the same class and mean entirely different
# things: on `favorites` a unique violation is "already favourited" and a
# foreign key failure is "no such project", and reporting the wrong one sends a
# client to correct the wrong half of its request.
#
# What is deliberately NOT distinguished is the tenant. A project from another
# workspace and a project that does not exist both break
# `favorites_project_fk`, so both arrive here and both produce the same
# NOT_FOUND. Telling them apart would require a lookup this service does not
# perform, and performing it would answer a question -- "does this id exist
# somewhere I cannot see" -- that no client may be allowed to ask.
_CONSTRAINT_ERRORS: dict[str, ValidationIssue] = {
    "saved_views_team_fk": ValidationIssue(
        field="teamId",
        code="NOT_FOUND",
        message="Team not found",
    ),
    # One message for three situations that must stay indistinguishable: no
    # such user anywhere, a real user in some other workspace, and a real user
    # whose membership here was revoked between the request being authorized
    # and this statement running. Naming which would let anyone holding a
    # workspace answer "is this person a member of yours?".
    "saved_views_creator_fk": ValidationIssue(
        field="workspaceSlug",
        code="NOT_MEMBER",
        message="You are no longer a member of this workspace",
    ),
    "favorites_member_fk": ValidationIssue(
        field="workspaceSlug",
        code="NOT_MEMBER",
        message="You are no longer a member of this workspace",
    ),
    "favorites_team_fk": ValidationIssue(
        field="teamId",
        code="NOT_FOUND",
        message="Team not found",
    ),
    "favorites_project_fk": ValidationIssue(
        field="projectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    "favorites_saved_view_fk": ValidationIssue(
        field="savedViewId",
        code="NOT_FOUND",
        message="Saved view not found",
    ),
    "favorites_team_key": ValidationIssue(
        field="teamId",
        code="ALREADY_FAVORITED",
        message="Already in your favorites",
    ),
    "favorites_project_key": ValidationIssue(
        field="projectId",
        code="ALREADY_FAVORITED",
        message="Already in your favorites",
    ),
    "favorites_saved_view_key": ValidationIssue(
        field="savedViewId",
        code="ALREADY_FAVORITED",
        message="Already in your favorites",
    ),
}

# The one answer for a view that does not exist, one in another workspace, one
# that is somebody else's personal view, and -- on the write paths -- one this
# viewer did not create. Four situations, one message, on purpose: a member who
# could tell "you may not edit this" from "there is no such view" could
# enumerate other people's private views by id.
_SAVED_VIEW_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Saved view not found",
)

_FAVORITE_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Favorite not found",
)


def _raise_mapped(error: asyncpg.PostgresError) -> NoReturn:
    """Translate a named constraint violation, or re-raise it untouched.

    The re-raise is the important half. A violation this service did not
    anticipate is a defect -- a missing NOT NULL, a constraint added by a
    later migration nobody taught this mapping about -- and turning it into a
    field error would tell a client to fix its input for a problem that is not
    in the input, while hiding the defect behind a 200.
    """
    issue = _CONSTRAINT_ERRORS.get(error.constraint_name or "")

    if issue is None:
        raise error

    raise ValidationError([issue]) from None


class SavedViewService:
    """Business rules for saved views.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service also
    owns connection acquisition and transaction boundaries.

    ## Why every method takes an AuthorizedWorkspaceScope

    Not a bare `WorkspaceScope`, which is what the other services in this
    package take. A saved view is readable when it is shared OR when the
    viewer created it, so every statement in `SavedViewRepository` needs a
    viewer id alongside the workspace -- and a `WorkspaceScope` carries only
    the tenant, which any caller who can spell a slug can obtain. Taking the
    authorized type means the viewer arrives from
    `app.graphql.scope.authorized_scope`, which is the only thing that
    produces one, rather than from an argument a resolver could fill in with
    a user id it read off a request body.

    The scope is threaded through as an argument rather than held on the
    instance, for the reason IssueService states: an instance attribute
    becomes an ambient current workspace that the next operation inherits
    without asking.

    ## Who may change a view

    Its creator, and nobody else -- enforced in the repository's WHERE clause
    rather than by a read-then-check here. Reading a shared view is something
    every member does; rewriting one is not, and "shared" is a grant of sight
    rather than a transfer of ownership. An admin override is deliberately
    absent rather than forgotten: it needs the role, a decision about whether
    it applies to personal views, and an audit trail, and none of those are
    cheaper to add later for having been guessed at now.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: SavedViewRepository,
        favorites: FavoriteRepository,
    ):
        self._pool = pool
        self._repository = repository

        # Deleting a view has to drop the favourites pointing at it, and SQL
        # against `favorites` belongs to the repository that owns that table.
        # A service reaching across to a second repository inside one
        # transaction is the shape this architecture is for; a saved-view
        # repository writing to `favorites` would not be.
        self._favorites = favorites

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        saved_view_id: UUID,
    ) -> SavedViewEntity | None:
        """One view this viewer may read, or nothing.

        "Not in this workspace", "does not exist" and "somebody else's
        personal view" are the same answer; the repository explains why the
        distinction must not be observable.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(
                connection,
                scope=scope,
                viewer_id=scope.user_id,
                saved_view_id=saved_view_id,
            )

    async def list(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        first: int,
        after: str | None,
        team_id: UUID | None | UnsetType = UNSET,
    ) -> SavedViewPage:
        """Forward keyset page of the views this viewer may read, by name.

        A single SELECT needs no explicit write transaction, so this acquires
        a connection without opening one.

        `team_id` has three states and needs all three: UNSET is every view,
        a UUID is that team's views, and None is the workspace-wide ones. A
        signature that could not tell the last two apart would answer "the
        views with no team" for a client that meant "any team".

        The cursor is not trusted to carry a workspace and could not be if it
        did: it is Base64 over JSON, readable and writable by anyone holding
        it. The scope and the viewer come from this call, so a cursor minted
        in one workspace -- or by another member -- and replayed here resumes
        at a position inside THIS viewer's own result set rather than in
        somebody else's.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list(
                connection,
                scope=scope,
                viewer_id=scope.user_id,
                filter_by_team=not isinstance(team_id, UnsetType),
                team_id=None if isinstance(team_id, UnsetType) else team_id,
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
            end_cursor = encode_name_cursor(last.name, last.id)

        return SavedViewPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    # ---------------------------------------------------------------- writes

    async def create(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        name: str,
        team_id: UUID | None,
        issue_filter: IssueFilter,
        order: IssueOrder,
        layout: str,
        grouping: str | None,
        subgrouping: str | None,
        visibility: str,
    ) -> SavedViewEntity:
        """Create one saved view, authored by the viewer.

        The author is `scope.user_id` and is never an argument. A client able
        to name the creator could forge one, and `created_by` is not
        decoration here -- it is half of the read predicate for a personal
        view and the whole of the write predicate for every view.

        Nothing checks that the viewer is still a member first:
        `saved_views_creator_fk` is composite over the workspace, so the same
        statement that writes the row is what refuses a non-member, with no
        window between the two for a membership to be revoked in.
        """
        self._validate_view(
            name=name,
            issue_filter=issue_filter,
            layout=layout,
            grouping=grouping,
            subgrouping=subgrouping,
            visibility=visibility,
        )

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block will
            # also carry the audit / sync / outbox writes.
            async with connection.transaction():
                try:
                    return await self._repository.create(
                        connection,
                        scope=scope,
                        creator_id=scope.user_id,
                        team_id=team_id,
                        name=name,
                        issue_filter=issue_filter,
                        order=order,
                        layout=layout,
                        grouping=grouping,
                        subgrouping=subgrouping,
                        visibility=visibility,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

    async def update(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        saved_view_id: UUID,
        # `str | None`, unlike `create` above, and see `_validate_view` for
        # why: a field GraphQL lets a patch omit is one it also lets a patch
        # send as null, and the null has to reach a validator that can refuse
        # it with a field error. Nothing downstream of that check ever sees
        # one. `layout` and `visibility` are the same case.
        name: str | None | UnsetType = UNSET,
        team_id: UUID | None | UnsetType = UNSET,
        issue_filter: IssueFilter | UnsetType = UNSET,
        order: IssueOrder | UnsetType = UNSET,
        layout: str | None | UnsetType = UNSET,
        grouping: str | None | UnsetType = UNSET,
        subgrouping: str | None | UnsetType = UNSET,
        visibility: str | None | UnsetType = UNSET,
    ) -> SavedViewEntity:
        """Apply a partial update and return the view as it now stands.

        Renaming, re-filtering, re-sorting, relaying out, regrouping and
        sharing are all this one method, because they are all one UPDATE and
        a client that changes two of them at once should not be sending two
        mutations that can half-fail. Sharing in particular is not a separate
        operation: `visibility='shared'` IS the share, and giving it its own
        mutation would be a second write path to the same column.

        `UNSET` and `None` are different arguments: UNSET leaves a field
        alone, None clears it. `name`, `layout` and `visibility` cannot be
        cleared -- the columns are NOT NULL -- so their types admit no None at
        all, and a client that sends null for any of them is refused by the
        GraphQL layer before this is called.

        A patch that sets nothing does not reach the database. The UPDATE
        would be harmless except for `updated_at = now()`, and stamping a row
        as modified because a client sent an empty form is a lie that
        propagates into every "recently changed" view built on that column.
        """
        self._validate_view(
            name=name,
            issue_filter=issue_filter,
            layout=layout,
            grouping=grouping,
            subgrouping=subgrouping,
            visibility=visibility,
        )

        patched = (
            name,
            team_id,
            issue_filter,
            order,
            layout,
            grouping,
            subgrouping,
            visibility,
        )

        if all(value is UNSET for value in patched):
            existing = await self.get_by_id(scope=scope, saved_view_id=saved_view_id)

            if existing is None:
                raise ValidationError([_SAVED_VIEW_NOT_FOUND])

            return existing

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    updated = await self._repository.update(
                        connection,
                        scope=scope,
                        viewer_id=scope.user_id,
                        saved_view_id=saved_view_id,
                        set_name=name is not UNSET,
                        name=None if isinstance(name, UnsetType) else name,
                        set_team_id=team_id is not UNSET,
                        team_id=None if isinstance(team_id, UnsetType) else team_id,
                        set_filter=issue_filter is not UNSET,
                        issue_filter=(
                            None
                            if isinstance(issue_filter, UnsetType)
                            else issue_filter
                        ),
                        set_order=order is not UNSET,
                        order=None if isinstance(order, UnsetType) else order,
                        set_layout=layout is not UNSET,
                        layout=None if isinstance(layout, UnsetType) else layout,
                        set_grouping=grouping is not UNSET,
                        grouping=None if isinstance(grouping, UnsetType) else grouping,
                        set_subgrouping=subgrouping is not UNSET,
                        subgrouping=(
                            None if isinstance(subgrouping, UnsetType) else subgrouping
                        ),
                        set_visibility=visibility is not UNSET,
                        visibility=(
                            None if isinstance(visibility, UnsetType) else visibility
                        ),
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

        if updated is None:
            # No row in this workspace with this id that this viewer wrote.
            # Which of those it was is deliberately not distinguished.
            raise ValidationError([_SAVED_VIEW_NOT_FOUND])

        return updated

    async def delete(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        saved_view_id: UUID,
    ) -> None:
        """Delete one view and the favourites pointing at it.

        The detachment is written out rather than delegated to ON DELETE
        CASCADE, for the reason `ProjectService.delete` gives: CASCADE would
        make `DELETE FROM saved_views WHERE id = ...` silently remove rows
        from other members' sidebars while reporting `DELETE 1`, and
        `favorites_saved_view_fk` is RESTRICT precisely so that cannot happen
        by accident.

        Those favourites belong to OTHER people, which is why this is the one
        operation that reaches outside the viewer's own rows. It is not a
        privilege escalation: the thing their shortcut pointed at is being
        destroyed by the person who owns it, so the alternative to removing
        the pointer is refusing the delete on behalf of everyone who ever
        favourited the view.

        All of it in one transaction, so a failure part way through leaves the
        view intact with its favourites still attached rather than half
        dismantled.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                # Ordered before the delete because the foreign key requires
                # it, and scoped by the workspace the caller was authorized
                # for. A view this viewer did not create is not deleted below,
                # so this clearing would be the one destructive thing that
                # happened -- which is why it is inside the transaction that
                # the raise below rolls back.
                await self._favorites.clear_for_saved_view(
                    connection,
                    scope=scope,
                    saved_view_id=saved_view_id,
                )

                deleted = await self._repository.delete(
                    connection,
                    scope=scope,
                    viewer_id=scope.user_id,
                    saved_view_id=saved_view_id,
                )

                if not deleted:
                    raise ValidationError([_SAVED_VIEW_NOT_FOUND])

    # ------------------------------------------------------------ validation

    @staticmethod
    def _validate_list(*, first: int, after: str | None) -> NameCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an
        expected input error rather than a parser exception. The rules and the
        codes are IssueService's, deliberately: two list endpoints that
        disagreed about the legal page size would be a contract a client has
        to learn twice.
        """
        issues: list[ValidationIssue] = []
        cursor: NameCursor | None = None

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
                cursor = decode_name_cursor(after)
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
    def _validate_view(
        *,
        # `str | None`, though the column is NOT NULL and clearing a name is
        # not an operation. GraphQL makes an input field required exactly when
        # it is non-null with no default, so a name that may be OMITTED from a
        # patch is a name that may also arrive as null -- and it has to be
        # representable here in order to be refused here, rather than reaching
        # a NOT NULL violation the client cannot read. The same reasoning
        # `IssuePatch` records for its first three fields.
        name: str | None | UnsetType,
        issue_filter: IssueFilter | UnsetType,
        layout: str | None | UnsetType,
        grouping: str | None | UnsetType,
        subgrouping: str | None | UnsetType,
        visibility: str | None | UnsetType,
    ) -> None:
        """Collect every violation, then raise once.

        Shared by create and update, which is what keeps a view that could be
        created from being one that could not be updated back into the same
        values. A field the caller did not mention is not validated -- there
        is nothing to validate -- so UNSET short-circuits each check rather
        than being coerced into some stand-in value.

        The filter is validated by `app.services.issues.filter_issues`, the
        same function `IssueService.list` runs on a filter arriving live. That
        reuse is the point of the whole feature: a filter that could be
        executed can be saved, a filter that could be saved can be executed,
        and there is one definition of which is which. Without it a view could
        store a priority of 9 -- accepted, then refused by the list it exists
        to produce, on a screen with nowhere to report it.

        Field order is deterministic so that clients can rely on it. The codes
        and messages are a public contract.
        """
        issues: list[ValidationIssue] = []

        # Validated as supplied -- never trimmed or rewritten. A name of three
        # spaces is a name the caller typed, and silently turning it into a
        # REQUIRED failure would report an error about input the client never
        # sent.
        if not isinstance(name, UnsetType):
            # `name is None` FIRST, and not merely for tidiness: without it
            # `len(None)` raises TypeError out of a validator, which the
            # transport masks as "Internal server error" -- so the one input
            # this branch exists to refuse would be the one it crashed on.
            if name is None or len(name) < NAME_MIN_LENGTH:
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

        if not isinstance(issue_filter, UnsetType):
            issues.extend(filter_issues(issue_filter))

        issues.extend(
            _vocabulary_issues("layout", layout, SAVED_VIEW_LAYOUTS, nullable=False)
        )
        issues.extend(
            _vocabulary_issues(
                "grouping", grouping, SAVED_VIEW_GROUPINGS, nullable=True
            )
        )
        issues.extend(
            _vocabulary_issues(
                "subgrouping", subgrouping, SAVED_VIEW_GROUPINGS, nullable=True
            )
        )
        issues.extend(
            _vocabulary_issues(
                "visibility", visibility, SAVED_VIEW_VISIBILITIES, nullable=False
            )
        )

        # Only checkable when BOTH arrive, which is why it is not folded into
        # the vocabulary checks above. A patch that sets only `subgrouping` is
        # validated against the stored `grouping` by
        # saved_views_subgrouping_requires_grouping, which raises
        # CheckViolationError -- an unexpected failure this service does not
        # map, and correctly so: the client cannot see the stored value, so
        # there is no field error that would tell it anything actionable.
        if (
            not isinstance(subgrouping, UnsetType)
            and subgrouping is not None
            and not isinstance(grouping, UnsetType)
            and (grouping is None or grouping == subgrouping)
        ):
            issues.append(
                ValidationIssue(
                    field="subgrouping",
                    code="INVALID",
                    message="Subgrouping requires a different grouping",
                )
            )

        if issues:
            raise ValidationError(issues)


def _vocabulary_issues(
    field: str,
    value: str | None | UnsetType,
    allowed: tuple[str, ...],
    *,
    nullable: bool,
) -> list[ValidationIssue]:
    """One closed-vocabulary check, shared by the four columns that have one.

    The legal values are named in the message. The CHECK constraints in
    migration 019 would reject these too, but as a CheckViolationError
    carrying the rendered constraint -- which is either masked (telling the
    client nothing) or forwarded (telling it about the schema). Neither is a
    usable answer to "which layouts may I send".

    Module-level rather than a method, for the reason `_filter_issues` in
    app/services/issues.py is: inside a class whose own `list` shadows the
    builtin, the annotation `list[ValidationIssue]` stops meaning what it
    says.
    """
    if isinstance(value, UnsetType):
        return []

    if value is None:
        if nullable:
            return []

        # Unreachable through GraphQL, which refuses a null for a non-null
        # input field before any resolver runs. Stated rather than assumed,
        # because REST and workers reach this function too.
        return [
            ValidationIssue(
                field=field,
                code="REQUIRED",
                message=f"{field.capitalize()} is required",
            )
        ]

    if value not in allowed:
        return [
            ValidationIssue(
                field=field,
                code="INVALID",
                message=f"{field.capitalize()} must be one of: {', '.join(allowed)}",
            )
        ]

    return []


class FavoriteService:
    """Business rules for one person's favourites in one workspace.

    Separate from SavedViewService because the two are independent: a
    favourite points at a team, a project or a saved view, and only the third
    of those has anything to do with saved views. What couples them is one
    statement -- `SavedViewService.delete` clears the favourites pointing at
    the view it destroys -- and that is a service reaching across to a second
    repository, which is the shape this architecture already has.

    Takes an AuthorizedWorkspaceScope for the reason SavedViewService does,
    and more sharply: a favourite is per-user, so a bare WorkspaceScope would
    leave the user id to be supplied by the caller -- and a caller that
    supplies a user id is a caller that can supply somebody else's.
    """

    def __init__(self, pool: asyncpg.Pool, repository: FavoriteRepository):
        self._pool = pool
        self._repository = repository

    async def list(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
    ) -> list[FavoriteEntity]:
        """This viewer's favourites in this workspace, in their own order.

        Bounded by FAVORITE_LIST_LIMIT, which the service states rather than
        the repository defaulting to.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list(
                connection,
                scope=scope,
                user_id=scope.user_id,
                limit=FAVORITE_LIST_LIMIT,
            )

    async def add(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        team_id: UUID | None = None,
        project_id: UUID | None = None,
        saved_view_id: UUID | None = None,
    ) -> FavoriteEntity:
        """Favourite exactly one thing.

        The "exactly one" is checked here rather than left to
        `favorites_one_target`, because a client that named two targets or
        none needs to be told which field to fix, and a CheckViolationError
        carrying a rendered constraint says nothing a UI can act on.

        Nothing is looked up first. The composite foreign keys refuse a target
        from another workspace as part of the insert, and the saved-view
        visibility guard is in the same statement -- so there is no window
        between a check and a write for the target to move tenants or be made
        private in.
        """
        targets = (team_id, project_id, saved_view_id)

        if sum(target is not None for target in targets) != 1:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="target",
                        code="INVALID",
                        message=(
                            "Exactly one of teamId, projectId or savedViewId is "
                            "required"
                        ),
                    )
                ]
            )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    favorite = await self._repository.add(
                        connection,
                        scope=scope,
                        user_id=scope.user_id,
                        team_id=team_id,
                        project_id=project_id,
                        saved_view_id=saved_view_id,
                    )
                except (
                    asyncpg.ForeignKeyViolationError,
                    asyncpg.UniqueViolationError,
                ) as error:
                    _raise_mapped(error)

        if favorite is None:
            # The insert selected no row, which happens for exactly one
            # reason: the saved view is one this viewer may not read. Reported
            # as the same NOT_FOUND a nonexistent id gets, because telling
            # them apart would confirm that another member has a private view
            # with that id.
            raise ValidationError(
                [
                    ValidationIssue(
                        field="savedViewId",
                        code="NOT_FOUND",
                        message="Saved view not found",
                    )
                ]
            )

        return favorite

    async def remove(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        favorite_id: UUID,
    ) -> None:
        """Take one thing out of this viewer's favourites.

        A favourite belonging to another member, one in another workspace and
        one that never existed are all the same NOT_FOUND: the repository's
        predicate carries the user as well as the workspace, so no statement
        here ever computes the difference.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                removed = await self._repository.remove(
                    connection,
                    scope=scope,
                    user_id=scope.user_id,
                    favorite_id=favorite_id,
                )

                if not removed:
                    raise ValidationError([_FAVORITE_NOT_FOUND])

    async def reorder(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        favorite_id: UUID,
        position: int,
    ) -> FavoriteEntity:
        """Move one favourite to a position in this viewer's own list.

        One row per call. Renumbering a whole list is the client calling this
        once per moved item, which is more round trips than a bulk reorder
        and is the shape that cannot half-apply: positions are neither unique
        nor required to be contiguous, so every intermediate state is a valid
        list rather than a broken one.
        """
        if position < POSITION_MIN:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="position",
                        code="OUT_OF_RANGE",
                        message=f"Position must be at least {POSITION_MIN}",
                    )
                ]
            )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                moved = await self._repository.set_position(
                    connection,
                    scope=scope,
                    user_id=scope.user_id,
                    favorite_id=favorite_id,
                    position=position,
                )

        if moved is None:
            raise ValidationError([_FAVORITE_NOT_FOUND])

        return moved
