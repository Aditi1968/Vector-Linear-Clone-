from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LabelEntity:
    """One label, as the product understands it.

    No `workspace_id`. A label is only ever read or written through a
    WorkspaceScope, so carrying the tenant here would be a second copy of a
    fact the caller already holds -- and a second copy is something that can
    disagree. `IssueEntity` omits it for the same reason.
    """

    id: UUID
    name: str
    color: str

    # The group this label sits under, or None for a label that sits under
    # none -- which is what most labels are, and a real state rather than a
    # missing value.
    #
    # Only the id. An entity that embedded the group would have to be loaded
    # with one, and every label read -- including the batched one behind
    # `Issue.labels` -- would then pay for a join whether or not the caller
    # wanted it. `IssueEntity` carries `project_id` for the same reason.
    #
    # The group's `exclusive` flag is deliberately NOT carried here, though
    # `labels.group_exclusive` holds a copy of it. That column exists so the
    # database can compute `exclusivity_key` from one row; it is storage
    # machinery, and a second place for the product to read exclusivity from
    # is a second place it can be read from the wrong one.
    group_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LabelGroupEntity:
    """A parent for labels, and -- optionally -- a rule about wearing them.

    `exclusive` is the whole reason this type exists rather than a `parent_id`
    on `LabelEntity`. A group that only nested labels would be a display
    concern; a group that can refuse the second label from itself on one issue
    is a constraint, and constraints need somewhere to live.

    Deliberately does not carry its labels. A group is read to be listed and
    to be named; the labels under it are a page of another list, and a type
    that held them would make every group read decide how many to hold.

    No `workspace_id`, for the reason `LabelEntity` gives: a group is only
    ever read or written through a WorkspaceScope, so carrying the tenant
    here would be a second copy of a fact the caller already holds.
    """

    id: UUID
    name: str

    # Whether an issue may wear more than one label from this group.
    #
    # The application's copy of `label_groups.exclusive`, and the copy a
    # reader may trust: it is read straight off the group's own row. The
    # `group_exclusive` column on `labels` is a THIRD copy that exists only so
    # PostgreSQL can compute a generated column from a single row, and nothing
    # above the repository layer ever reads it.
    exclusive: bool

    created_at: datetime
    updated_at: datetime
