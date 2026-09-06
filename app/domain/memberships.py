from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WorkspaceMembershipEntity:
    """One `workspace_members` row, joined to the workspace it names.

    Flat rather than holding a nested workspace entity. Every read that
    produces one of these is a single join -- a membership is only ever
    interesting alongside the workspace it grants access to -- so the shape
    matches the row that comes back, and a caller cannot be handed a
    membership whose workspace half was fetched separately and may describe a
    different moment.

    `role` is a plain str holding one of app.domain.tenancy.WORKSPACE_ROLES;
    see the note there on why the domain does not model it as an enum.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    workspace_id: UUID
    workspace_slug: str
    workspace_name: str
    user_id: UUID
    role: str
    created_at: datetime
