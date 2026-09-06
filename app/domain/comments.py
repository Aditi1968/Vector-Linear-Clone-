from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CommentEntity:
    """One comment on one issue.

    `issue_id` is carried and `workspace_id` is not, and the asymmetry is the
    point. The issue is what a comment is *about*, so a reader holding a
    comment needs it. The workspace is the scope the read was made in, which
    the caller already supplied; repeating it here would let an entity claim a
    tenant that disagreed with the scope that produced it.

    `edited_at` is None for a comment that has never been edited, which is a
    different claim from `updated_at`: any write touches updated_at, including
    ones the author did not make.
    """

    id: UUID
    issue_id: UUID
    author_id: UUID
    body: str
    edited_at: datetime | None
    created_at: datetime
    updated_at: datetime
