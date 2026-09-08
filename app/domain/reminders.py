"""The issues a sweep has just taken responsibility for warning about.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DueIssue:
    """One issue whose due date this pass has claimed, and its tenant.

    Two ids and nothing else, which is the whole surface a reminder needs: the
    recipients are derived by `NotificationRepository.notify_about_issue` from
    the issue's own row and its subscribers, so there is no field here through
    which a caller could name who hears about something -- the property
    migration 027 makes structural for the Slack pipeline, kept for this one.

    `workspace_id` travels WITH the issue rather than being a parameter the
    caller supplies alongside it. The sweep has no tenant of its own, so the
    only trustworthy workspace is the one the database matched on the row, and
    a pair means there is no way to hold one issue's id beside another
    workspace's scope.

    A dataclass rather than a bare tuple, because `(workspace_id, issue_id)`
    and `(issue_id, workspace_id)` are both two UUIDs and neither a type
    checker nor a reader can tell them apart at a call site.
    """

    workspace_id: UUID
    issue_id: UUID
