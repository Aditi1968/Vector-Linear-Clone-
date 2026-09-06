from dataclasses import dataclass
from datetime import date, datetime
from typing import Final
from uuid import UUID


# The five states migrations/009_projects.sql admits, in the order a project
# moves through them. The tuple is ordered rather than a set because the order
# is the one the product shows them in; membership is what the validator uses.
#
# This list and `projects_state_check` are two statements of one rule, and they
# have to be changed together. That is not a duplication that can be designed
# away: the database has to reject a bad state whoever writes it, and the
# service has to reject one without spending a round trip and without turning a
# CheckViolationError into a user-facing message. tests/test_projects_db.py
# asserts the two agree.
PROJECT_STATES: Final[tuple[str, ...]] = (
    "planned",
    "started",
    "paused",
    "completed",
    "canceled",
)

# What a project starts in when the caller does not say. Deliberately here and
# not a column DEFAULT: a default in the schema outlives the migration, so an
# insert that forgot `state` would succeed quietly instead of failing, and the
# product rule would then live in two places that can drift apart. See the note
# on `state` in migrations/009_projects.sql.
DEFAULT_PROJECT_STATE: Final = "planned"


@dataclass(frozen=True, slots=True)
class ProjectEntity:
    """One project, as the rest of the application sees it.

    No `workspace_id`, matching IssueEntity. The workspace is how an operation
    is scoped, not something an entity carries around afterwards: a caller that
    could read it off an entity would eventually pass it back down as the scope
    for the next call, which is how a tenant boundary stops being an argument
    the caller has to supply and becomes one it can launder.

    `team_ids` is part of the entity rather than something fetched per project.
    "Which teams is this project shared with" is the question this whole table
    exists to answer, so a shape that answers it only through a second round
    trip would make the headline capability the expensive path. The repository
    aggregates it in the same statement; see ProjectRepository.
    """

    id: UUID
    name: str
    description: str | None
    state: str
    target_date: date | None
    team_ids: tuple[UUID, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectMilestoneEntity:
    """One milestone, which is only ever meaningful inside its project.

    `project_id` is carried because a milestone read on its own is
    indistinguishable from another project's without it -- and because
    `issues_milestone_fk` references (workspace_id, project_id, id), so the
    project is part of a milestone's identity as far as the schema is
    concerned, not merely a parent pointer.
    """

    id: UUID
    project_id: UUID
    name: str
    target_date: date | None
    position: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectPage:
    """A forward keyset page of projects.

    The same three fields as IssuePage and for the same reasons: the nodes, a
    flag the caller cannot compute for itself, and the position to resume from.
    """

    nodes: list[ProjectEntity]
    has_next_page: bool
    end_cursor: str | None
