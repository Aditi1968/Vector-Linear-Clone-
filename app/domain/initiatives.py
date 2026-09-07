"""Initiatives, their hierarchy and the updates posted against them.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final
from uuid import UUID


# The four states migrations/022_initiatives.sql admits, in the order an
# initiative moves through them. The tuple is ordered rather than a set because
# the order is the one the product shows them in; membership is what the
# validator uses.
#
# This list and `initiatives_status_check` are two statements of one rule and
# have to be changed together, for the reason app.domain.projects gives about
# PROJECT_STATES. tests/test_migration_022_db.py asserts the two agree.
INITIATIVE_STATUSES: Final[tuple[str, ...]] = (
    "planned",
    "active",
    "completed",
    "canceled",
)

# What an initiative starts in when the caller does not say. Deliberately here
# and not a column DEFAULT: a default in the schema outlives the migration, so
# an insert that forgot `status` would succeed quietly instead of failing.
DEFAULT_INITIATIVE_STATUS: Final = "planned"

# How many edges may sit above a top-level initiative, so five tiers in all.
#
# A product bound rather than a technical one, and the reason it is stated at
# all is that it is what makes the cycle guard's recursion terminate on a
# graph an attacker can grow: InitiativeRepository.inspect_parenting walks at
# most this many rows up and this many down, so the cost of the check does not
# depend on how many initiatives a workspace holds.
#
# Raising it is a one-line change here AND a re-reading of that walk: the two
# bounds in the recursive terms are written in terms of this number precisely
# so they cannot be raised independently of it.
MAX_INITIATIVE_DEPTH: Final = 4


@dataclass(frozen=True, slots=True)
class InitiativeEntity:
    """One initiative, as the rest of the application sees it.

    No `workspace_id`, matching IssueEntity and ProjectEntity: the workspace is
    how an operation is scoped, not something an entity carries around
    afterwards. A caller that could read it off an entity would eventually pass
    it back down as the scope for the next call, which is how a tenant boundary
    stops being an argument the caller has to supply and becomes one it can
    launder.

    `project_ids` and `child_initiative_ids` are part of the entity rather than
    fetched per initiative, for the reason ProjectEntity carries `team_ids`:
    "which projects add up to this goal" and "what sits under it" are the two
    questions this whole feature exists to answer, so a shape that answered
    them only through a second round trip would make the headline capability
    the expensive path. The repository aggregates both in the same statement.
    """

    id: UUID
    name: str
    description: str | None
    status: str

    # How it is going, as most recently reported, or None if nobody has
    # reported yet. A string and not an enum for the reason
    # `AuthorizedWorkspaceScope.role` is one: the value's authority comes from
    # the CHECK that admitted the row, and the transport keeps its own
    # vocabulary and converts at the boundary.
    health: str | None

    target_date: date | None

    # The workspace member accountable for this initiative, or None. A user id
    # and not a UserEntity, for the reason ProjectEntity gives about `lead_id`:
    # `initiatives_owner_fk` guarantees the id names a member of this
    # initiative's workspace, and loading the account is a transport concern.
    owner_id: UUID | None

    parent_initiative_id: UUID | None

    project_ids: tuple[UUID, ...]
    child_initiative_ids: tuple[UUID, ...]

    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InitiativeUpdateEntity:
    """One posted update, which is only ever meaningful inside its initiative.

    `initiative_id` is carried for the reason ProjectMilestoneEntity carries
    `project_id`: an update read on its own is indistinguishable from another
    initiative's without it.

    No `updated_at`, matching the table: an update is a statement somebody made
    at a moment and there is no edit path, so a second timestamp could only
    ever equal the first.
    """

    id: UUID
    initiative_id: UUID
    health: str
    body: str
    author_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InitiativePage:
    """A forward keyset page of initiatives.

    The same three fields as IssuePage and ProjectPage and for the same
    reasons: the nodes, a flag the caller cannot compute for itself, and the
    position to resume from.
    """

    nodes: list[InitiativeEntity]
    has_next_page: bool
    end_cursor: str | None


@dataclass(frozen=True, slots=True)
class ParentingCheck:
    """What one bounded walk of the hierarchy found, before a re-parent.

    Three numbers rather than a bare "may I?", because the service reports
    two different refusals from them and a boolean would collapse the pair.

    `creates_cycle` is the proposed child appearing among the proposed parent's
    ancestors -- which includes the parent itself, so a self-parent that
    somehow reached this far is caught here too rather than only by
    `initiatives_parent_not_self`.

    `parent_depth` is how many edges already sit above the proposed parent, and
    `subtree_height` how many sit below the deepest descendant of the initiative
    being moved. Their sum plus one is the depth the tree would reach, which is
    the number MAX_INITIATIVE_DEPTH is compared against -- so a three-deep
    sub-tree moving under a three-deep parent is refused even though neither
    half is itself too deep.

    Both walks are truncated at MAX_INITIATIVE_DEPTH, so either number may be
    a floor rather than an exact count. That is safe in one direction only and
    deliberately so: a truncated count can only ever be at or above the limit,
    so it can produce a refusal that is correct and never an approval that is
    not.
    """

    creates_cycle: bool
    parent_depth: int
    subtree_height: int

    def resulting_depth(self) -> int:
        """How deep the tree would be once the move is applied."""
        return self.parent_depth + 1 + self.subtree_height
