"""What happened to an issue, as the product talks about it.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.

Distinct from `app.domain.comments` and from `app.domain.notifications`, and
the three are not layers of one thing: a comment is what a person wrote, an
activity row is what the system recorded happening, and a notification is what
one person still has to look at. See migrations/012_activity_notifications.sql.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.issues import IssueEntity


class ActivityKind(StrEnum):
    """Every event the history records, and the application's copy of
    `issue_activity_kind_known` in migration 012.

    A StrEnum because the member IS the stored spelling, so nothing converts
    at the repository boundary and a kind cannot reach the database in a
    casing the CHECK refuses. Two copies exist for the reason
    `WORKSPACE_ROLES` gives: the database has to refuse an unknown kind
    whether or not the write came through this code, and this code has to know
    the vocabulary without asking the database.
    """

    CREATED = "created"
    TITLE_CHANGED = "title_changed"
    STATE_CHANGED = "state_changed"
    PRIORITY_CHANGED = "priority_changed"
    ASSIGNEE_CHANGED = "assignee_changed"
    ARCHIVED = "archived"
    COMMENTED = "commented"
    LABEL_ATTACHED = "label_attached"
    LABEL_DETACHED = "label_detached"
    RELATION_ADDED = "relation_added"
    PROJECT_CHANGED = "project_changed"
    CYCLE_CHANGED = "cycle_changed"


@dataclass(frozen=True, slots=True)
class ActivityEntity:
    """One thing that happened, read back.

    `actor_id` is None for a system action and for an action whose account has
    since been deleted -- `issue_activity_actor_fk` nulls it on the way out.
    A reader cannot tell the two apart, and does not need to: both mean the
    same thing to the timeline, which is "nobody to name here".

    `from_value` and `to_value` are opaque text whose meaning depends on
    `kind`. Rendering them is the client's job, because it is the layer that
    knows how to turn a workflow-state id into a name.
    """

    id: UUID
    issue_id: UUID
    actor_id: UUID | None
    kind: ActivityKind
    from_value: str | None
    to_value: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ActivityPage:
    nodes: list[ActivityEntity]
    has_next_page: bool
    end_cursor: str | None


@dataclass(frozen=True, slots=True)
class IssueSnapshot:
    """The fields the history compares, as they were before a write.

    Not an `IssueEntity`: this is read under a row lock immediately before an
    update, and carrying the other dozen columns would mean a wider SELECT and
    a second nearly-identical entity in every caller's hands -- one of which
    is stale by definition, which is exactly the confusion a smaller type
    prevents. Every field here is one an activity row can report a change to.
    """

    title: str
    priority: int
    workflow_state_id: UUID
    assignee_id: UUID | None
    project_id: UUID | None
    cycle_id: UUID | None

    @classmethod
    def of(cls, entity: IssueEntity) -> "IssueSnapshot":
        """The same six fields, taken from an issue a write just returned.

        The "after" side never needs a second read: every write here returns
        the row it produced. Narrowing it through this classmethod rather than
        comparing entities directly is what keeps `changes` from ever
        comparing `updated_at`, which moves on every write and would make
        every update report a change.
        """
        return cls(
            title=entity.title,
            priority=entity.priority,
            workflow_state_id=entity.workflow_state_id,
            assignee_id=entity.assignee_id,
            project_id=entity.project_id,
            cycle_id=entity.cycle_id,
        )


def _text(value: object | None) -> str | None:
    """One activity value as the column stores it, or None.

    Uniform `str()` over uuid, int and str rather than a per-kind conversion,
    because the columns are opaque to the database and to every query that
    reads them -- see migration 012 on why they are two TEXT columns and not a
    JSONB payload.
    """
    return None if value is None else str(value)


def changes(
    before: IssueSnapshot,
    after: IssueSnapshot,
) -> list[tuple[ActivityKind, str | None, str | None]]:
    """The (kind, from, to) triples one update is worth recording.

    Only fields that actually MOVED. An update that rewrites a title to the
    same string is a write the database performs and not an event the history
    should claim, and a timeline full of "changed the title from X to X" is
    one nobody reads.

    Comparison is on the value, so clearing an assignee (to None) is a change
    and re-clearing an already-empty one is not.

    The order is fixed -- title, state, priority, assignee, project, cycle --
    so that two field changes in one request produce two rows in an order that
    does not depend on dict iteration. They share a `created_at` to the
    microsecond, so `id` is what breaks the tie in the timeline, and uuidv7
    makes that tie-break the insertion order.
    """
    pairs = (
        (ActivityKind.TITLE_CHANGED, before.title, after.title),
        (
            ActivityKind.STATE_CHANGED,
            before.workflow_state_id,
            after.workflow_state_id,
        ),
        (ActivityKind.PRIORITY_CHANGED, before.priority, after.priority),
        (ActivityKind.ASSIGNEE_CHANGED, before.assignee_id, after.assignee_id),
        (ActivityKind.PROJECT_CHANGED, before.project_id, after.project_id),
        (ActivityKind.CYCLE_CHANGED, before.cycle_id, after.cycle_id),
    )

    return [(kind, _text(old), _text(new)) for kind, old, new in pairs if old != new]
