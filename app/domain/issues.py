from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class IssueEntity:
    id: UUID
    title: str
    description: str | None
    priority: int
    # Where this issue sits in the workspace's project plan, if anywhere.
    # Both are None for the great majority of issues, and that is a real
    # state rather than missing data.
    #
    # `milestone_id` is never set while `project_id` is None:
    # `issues_milestone_requires_project` refuses that row, because a
    # milestone only means anything inside the project that owns it. A
    # reader can therefore take a non-None milestone as implying a project
    # without checking for it.
    project_id: UUID | None
    milestone_id: UUID | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
