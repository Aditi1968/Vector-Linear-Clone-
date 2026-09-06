from uuid import UUID

from strawberry.fastapi import BaseContext

from app.db import get_pool
from app.repositories.issues import IssueRepository
from app.repositories.memberships import MembershipRepository
from app.services.issues import IssueService
from app.services.memberships import MembershipService


class VectorContext(BaseContext):
    """Per-request GraphQL context."""

    def __init__(
        self,
        issue_service: IssueService,
        membership_service: MembershipService | None = None,
        viewer_user_id: UUID | None = None,
    ):
        super().__init__()

        self.issue_service = issue_service
        self.membership_service = membership_service

        # Who is making this request, or None when nobody has been
        # identified. The authenticated-session layer is what will set it;
        # until that lands this is None on every request, and every resolver
        # that reads it answers UNAUTHENTICATED. That is the intended
        # behaviour of a half-built auth stack rather than a gap: the field
        # exists so that "the viewer is unknown" is a value the schema can
        # act on, instead of a question no resolver knows to ask.
        #
        # It is set here, on the server, from the request's own credentials.
        # A resolver may read it; nothing may accept it as an argument. See
        # `app.graphql.queries.memberships`.
        self.viewer_user_id = viewer_user_id


async def get_context() -> VectorContext:
    # The pool is owned by the FastAPI lifespan; this only borrows it.
    # Never call connect() or create a pool here.
    pool = get_pool()

    return VectorContext(
        issue_service=IssueService(
            pool=pool,
            repository=IssueRepository(),
        ),
        membership_service=MembershipService(
            pool=pool,
            repository=MembershipRepository(),
        ),
    )
