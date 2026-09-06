from strawberry.fastapi import BaseContext

from app.db import get_pool
from app.repositories.issues import IssueRepository
from app.repositories.teams import TeamRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.issues import IssueService
from app.services.teams import TeamService
from app.services.workspaces import WorkspaceService


class VectorContext(BaseContext):
    """Per-request GraphQL context."""

    def __init__(
        self,
        issue_service: IssueService,
        team_service: TeamService,
        workspace_service: WorkspaceService,
    ):
        super().__init__()

        self.issue_service = issue_service
        self.team_service = team_service
        self.workspace_service = workspace_service


async def get_context() -> VectorContext:
    # The pool is owned by the FastAPI lifespan; this only borrows it.
    # Never call connect() or create a pool here.
    pool = get_pool()

    return VectorContext(
        issue_service=IssueService(
            pool=pool,
            repository=IssueRepository(),
        ),
        team_service=TeamService(
            pool=pool,
            repository=TeamRepository(),
        ),
        workspace_service=WorkspaceService(
            pool=pool,
            repository=WorkspaceRepository(),
        ),
    )
