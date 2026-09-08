"""The prefilled shapes a new issue can be filed from.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.

Two types rather than one, and the split is the same one `IssuePatch` makes
against `IssueEntity`: a draft is what a caller SUPPLIES, an entity is what the
database RETURNED. Collapsing them would give the write path an `id`,
`created_at` and `updated_at` a client could name -- which is how a mutation
grows a way to forge a template's age, or to address one by id it was never
given.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.recurrence import RecurrenceEntity


@dataclass(frozen=True, slots=True)
class IssueTemplateDraft:
    """Everything one save of a template says.

    Every default is optional and None means "this template has no opinion
    about that field", never "clear it". There is no UNSET machinery here and
    no patch type, deliberately: a template is a small record edited whole in a
    form, so a save REPLACES it. That makes clearing a field the same operation
    as setting one -- send it as null -- and spares this layer the three-valued
    logic `IssuePatch` needs for a resource that is edited a field at a time by
    several people at once.

    `label_ids` is a tuple rather than a list so the dataclass stays hashable
    and cannot be mutated by whatever it is handed to. Order is not meaningful:
    the join table has no position column, and labels are rendered in name
    order wherever they are shown.
    """

    name: str
    team_id: UUID | None = None
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    estimate: int | None = None
    assignee_id: UUID | None = None
    project_id: UUID | None = None
    cycle_id: UUID | None = None
    label_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class IssueTemplateEntity:
    """One stored template, read back.

    `team_id` is None for a template the whole workspace shares, which is an
    ordinary state and not a missing value -- see the migration's note on the
    column.

    Every id here is one the database has already confirmed belongs to this
    template's workspace, because each is held by a composite foreign key
    through `workspace_id`. That is worth knowing at this layer because it is
    what the apply path relies on NOT relying on: it re-checks anyway, by
    filing the issue through the same constraints, so a value that somehow got
    in is refused a second time rather than trusted once.
    """

    id: UUID
    team_id: UUID | None
    name: str
    title: str | None
    description: str | None
    priority: int | None
    estimate: int | None
    assignee_id: UUID | None
    project_id: UUID | None
    cycle_id: UUID | None
    label_ids: tuple[UUID, ...]

    # The schedule that files this template by itself, or None for a template
    # somebody applies by hand -- which is nearly all of them.
    #
    # On the ENTITY and deliberately not on `IssueTemplateDraft`. A save
    # REPLACES the record, so a recurrence carried in the draft would be
    # cleared by every edit of the template's title -- and the two are edited
    # in different places for different reasons: the shape in a form, the
    # schedule in a switch beside it. `issueTemplateRecurrenceSet` and its
    # clear are their own mutations for that reason, and `TemplateRepository`
    # writes the row from its own statement.
    recurrence: RecurrenceEntity | None

    created_at: datetime
    updated_at: datetime
