from strawberry.fastapi import BaseContext

from app.db import get_pool
from app.graphql.tenancy import RequestTenant
from app.repositories.issues import IssueRepository
from app.repositories.teams import TeamRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.issues import IssueService
from app.services.teams import TeamService
from app.services.workspaces import WorkspaceService


class VectorContext(BaseContext):
    """Per-request GraphQL context."""

    def __init__(self, issue_service: IssueService, tenant: RequestTenant):
        super().__init__()

        self.issue_service = issue_service

        # Required, not defaulted. A context that could be built without a
        # tenant would let a resolver reach the services with no workspace
        # to give them, and the first sign of it would be a TypeError in
        # production rather than a failure to construct.
        self.tenant = tenant


async def get_context() -> VectorContext:
    # The pool is owned by the FastAPI lifespan; this only borrows it.
    # Never call connect() or create a pool here.
    pool = get_pool()

    return VectorContext(
        issue_service=IssueService(
            pool=pool,
            repository=IssueRepository(),
        ),
        # Built here rather than resolved here: nothing in this function
        # touches the database. Constructing a context is on the path of
        # every request, including the malformed ones a query never runs
        # for, so the workspace lookup happens in the resolver that needs
        # it and not once per HTTP request regardless.
        tenant=RequestTenant(
            workspace_service=WorkspaceService(
                pool=pool,
                repository=WorkspaceRepository(),
            ),
            team_service=TeamService(
                pool=pool,
                repository=TeamRepository(),
            ),
        ),
    )
