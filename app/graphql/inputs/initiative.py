from datetime import date
from uuid import UUID

import strawberry

from app.graphql.types.health import HealthType
from app.graphql.types.initiative import InitiativeStatusType


# Every update input below spells its optional fields `T | None =
# strawberry.UNSET`, and the two halves mean different things -- the argument
# app/graphql/inputs/project.py makes in full. `strawberry.UNSET` as the
# default is what makes an omitted field arrive as UNSET rather than as None,
# which is the only way a partial update can tell "leave this alone" from "set
# this to null"; the `| None` is what lets a client say null at all.


@strawberry.input
class InitiativeCreateInput:
    # A slug and not a workspace id, deliberately. CLAUDE.md forbids trusting a
    # workspace id from the frontend: the slug is a public string that selects
    # WHAT is being asked about, and `app.graphql.scope` decides whether the
    # session behind the request may act there.
    workspace_slug: str

    name: str
    description: str | None = None
    # A default here and not in the schema. migrations/022_initiatives.sql
    # deliberately gives `initiatives.status` no column default, so something
    # has to choose; the GraphQL default is visible in the SDL a client reads,
    # which a column default is not.
    status: InitiativeStatusType = InitiativeStatusType.PLANNED
    target_date: date | None = None

    # The one user id this API accepts as an argument here, and the exception
    # is worth stating because the rule it bends is real: a client may never
    # name WHO IS ASKING -- that comes from the session. This names a different
    # thing, a value being stored on a row, the same kind of argument as `name`.
    # It is safe to accept because it is not trusted: `initiatives_owner_fk`
    # refuses any id that is not a member of this workspace, so the worst a
    # forged one achieves is a NOT_MEMBER field error.
    owner_id: UUID | None = None

    # No `parentInitiativeId`, deliberately. Nesting is `initiativeSetParent`,
    # which is the only path that takes the lock and runs the cycle and depth
    # guard; accepting one here would be a second writer of the parent column
    # outside that guard, which migrations/022_initiatives.sql names as exactly
    # how the guarantee is lost with no failing test to say so.

    # And no `health`. Health arrives by posting an update, so that every value
    # the board renders has a body and an author behind it.


@strawberry.input
class InitiativeUpdateInput:
    workspace_slug: str
    id: UUID
    name: str | None = strawberry.UNSET
    description: str | None = strawberry.UNSET
    status: InitiativeStatusType | None = strawberry.UNSET
    target_date: date | None = strawberry.UNSET
    # UNSET-defaulted like the rest, and here the three cases are the whole
    # feature: omitted leaves the owner alone, an id reassigns it, and an
    # explicit null takes the owner off the initiative.
    owner_id: UUID | None = strawberry.UNSET


@strawberry.input
class InitiativeDeleteInput:
    workspace_slug: str
    id: UUID


@strawberry.input
class InitiativeProjectInput:
    """The argument of both project-link mutations.

    One input for add and remove, because the two operations name exactly the
    same pair -- the asymmetry `ProjectTeamInput` warns about: a client that
    can express an association it cannot express the removal of has an
    initiative it cannot get back out of a state.
    """

    workspace_slug: str
    initiative_id: UUID
    project_id: UUID


@strawberry.input
class InitiativeSetParentInput:
    """Nest one initiative under another.

    Two ids and no null: clearing a parent is `initiativeClearParent`, a
    separate mutation, rather than this one with `parentInitiativeId: null`.
    The two operations have different rules -- attaching runs a lock, a cycle
    walk and a depth check, detaching runs none of them -- and one mutation
    whose guarantees depended on whether an argument was null would be a
    contract nobody could read off the schema.
    """

    workspace_slug: str
    initiative_id: UUID
    parent_initiative_id: UUID


@strawberry.input
class InitiativeClearParentInput:
    workspace_slug: str
    initiative_id: UUID


@strawberry.input
class InitiativeUpdatePostInput:
    """Post an update: how it is going, and why.

    No `authorId`. The author is the session's user, read off the authorized
    scope, and is not something a client may assert about itself -- the same
    rule `CommentCreateInput` follows, and `initiative_updates_author_fk` is
    what makes it true of the database rather than merely of a resolver.
    """

    workspace_slug: str
    initiative_id: UUID
    health: HealthType
    body: str
