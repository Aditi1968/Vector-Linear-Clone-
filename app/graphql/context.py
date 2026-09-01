from strawberry.fastapi import BaseContext

from app.db import get_pool
from app.repositories.issues import IssueRepository
from app.services.issues import IssueService


class VectorContext(BaseContext):
    """Per-request GraphQL context."""

    def __init__(self, issue_service: IssueService):
        super().__init__()

        self.issue_service = issue_service


async def get_context() -> VectorContext:
    # The pool is owned by the FastAPI lifespan; this only borrows it.
    # Never call connect() or create a pool here.
    pool = get_pool()

    return VectorContext(
        issue_service=IssueService(
            pool=pool,
            repository=IssueRepository(),
        ),
    )
