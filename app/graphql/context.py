import asyncio

from strawberry.fastapi import BaseContext

from app.config import Environment, get_settings
from app.db import get_pool
from app.domain.auth import UserEntity
from app.graphql.tenancy import RequestTenant
from app.http_cookies import read_session_token
from app.repositories.issues import IssueRepository
from app.repositories.sessions import SessionRepository
from app.repositories.teams import TeamRepository
from app.repositories.users import UserRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.auth import AuthService
from app.services.issues import IssueService
from app.services.passwords import Argon2PasswordHasher
from app.services.teams import TeamService
from app.services.workspaces import WorkspaceService


class VectorContext(BaseContext):
    """Per-request GraphQL context."""

    def __init__(
        self,
        issue_service: IssueService,
        auth_service: AuthService,
        team_service: TeamService,
        workspace_service: WorkspaceService,
        tenant: RequestTenant,
        environment: Environment,
    ):
        super().__init__()

        self.issue_service = issue_service
        self.auth_service = auth_service

        # The same two service objects `tenant` resolves through, exposed
        # directly for the resolvers that ask about teams and workspaces as
        # entities rather than as this request's scope. Shared instances,
        # not second copies: one request gets one of each.
        self.team_service = team_service
        self.workspace_service = workspace_service

        # Required, not defaulted. A context that could be built without a
        # tenant would let a resolver reach the services with no workspace
        # to give them, and the first sign of it would be a TypeError in
        # production rather than a failure to construct.
        self.tenant = tenant

        # Carried because the Set-Cookie policy depends on it: Secure is only
        # legal where the deployment speaks https. Passed in rather than read
        # from settings at the point of use, so that one request cannot end
        # up writing a cookie under one environment's rules and clearing it
        # under another's.
        self.environment = environment

        self._viewer: asyncio.Future[UserEntity | None] | None = None

    def session_token(self) -> str | None:
        """The raw token this request presented, if it presented one.

        `self.request` is set by strawberry's FastAPI integration after this
        object is constructed, so it is None for a context built by hand --
        which is a context with no request and therefore no token, not an
        error.
        """
        if self.request is None:
            return None

        return read_session_token(self.request)

    def viewer(self) -> "asyncio.Future[UserEntity | None]":
        """Who is making this request, resolved at most once.

        Memoised as a future rather than as a value, because GraphQL resolves
        sibling fields concurrently: two protected fields in one document
        would otherwise each start their own session lookup, and both would
        stamp `last_used_at` on the same row. Awaiting one shared future
        makes it one query per request however many fields ask.

        Deliberately not resolved when the context is built. An anonymous
        query -- and the introspection GraphiQL sends on every page load --
        would then pay for a database round trip to establish that nobody is
        signed in.

        Returns the user, or None. It never says *why* it is None; see
        AuthService.authenticate.
        """
        if self._viewer is None:
            self._viewer = asyncio.ensure_future(self._resolve_viewer())

        return self._viewer

    async def _resolve_viewer(self) -> UserEntity | None:
        return await self.auth_service.authenticate(self.session_token())


async def get_context() -> VectorContext:
    # The pool is owned by the FastAPI lifespan; this only borrows it.
    # Never call connect() or create a pool here.
    pool = get_pool()

    # Resolved per request rather than at import, which is what keeps this
    # module importable without a configured environment. get_settings is
    # lru_cached, so this is a dict lookup after the first request.
    environment = get_settings().environment

    # Constructed once and handed to both the context and the tenant. Two
    # instances would be two objects answering the same question over the
    # same pool, and any caching either one grows later would then be per
    # copy rather than per request.
    workspace_service = WorkspaceService(pool=pool, repository=WorkspaceRepository())
    team_service = TeamService(pool=pool, repository=TeamRepository())

    return VectorContext(
        issue_service=IssueService(
            pool=pool,
            repository=IssueRepository(),
        ),
        auth_service=AuthService(
            pool=pool,
            users=UserRepository(),
            sessions=SessionRepository(),
            hasher=Argon2PasswordHasher(),
        ),
        team_service=team_service,
        workspace_service=workspace_service,
        # Built here rather than resolved here: nothing in this function
        # touches the database. Constructing a context is on the path of
        # every request, including the malformed ones a query never runs
        # for, so the workspace lookup happens in the resolver that needs
        # it and not once per HTTP request regardless.
        tenant=RequestTenant(
            workspace_service=workspace_service,
            team_service=team_service,
        ),
        environment=environment,
    )
