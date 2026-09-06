import asyncio
import functools

from strawberry.dataloader import DataLoader
from strawberry.fastapi import BaseContext

from app.config import Environment, get_settings
from app.db import get_pool
from app.domain.auth import UserEntity
from app.domain.labels import LabelEntity
from app.graphql.loaders.cycles import CycleLoader
from app.graphql.loaders.labels import IssueLabelKey, issue_label_loader
from app.graphql.loaders.projects import (
    build_project_loader,
    build_project_milestones_loader,
)
from app.http_cookies import read_session_token
from app.repositories.comments import CommentRepository
from app.repositories.cycles import CycleRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.issues import IssueRepository
from app.repositories.labels import LabelRepository
from app.repositories.memberships import MembershipRepository
from app.repositories.projects import ProjectRepository
from app.repositories.relations import RelationRepository
from app.repositories.sessions import SessionRepository
from app.repositories.teams import TeamRepository
from app.repositories.users import UserRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.auth import AuthService
from app.services.comments import CommentService
from app.services.cycles import CycleService
from app.services.issues import IssueService
from app.services.labels import LabelService
from app.services.memberships import MembershipService
from app.services.passwords import Argon2PasswordHasher
from app.services.projects import ProjectService
from app.services.relations import RelationService
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
        membership_service: MembershipService,
        label_service: LabelService,
        comment_service: CommentService,
        cycle_service: CycleService,
        project_service: ProjectService,
        relation_service: RelationService,
        environment: Environment,
    ):
        super().__init__()

        self.issue_service = issue_service
        self.auth_service = auth_service
        self.membership_service = membership_service
        self.label_service = label_service
        self.comment_service = comment_service
        self.cycle_service = cycle_service
        self.project_service = project_service

        # Built here rather than in `get_context` so that a context assembled
        # by hand -- a test, a worker -- gets working loaders from the service
        # it was given, instead of two slots it has to remember to fill in
        # agreement with each other.
        #
        # One pair per request, which is the whole contract of a DataLoader:
        # it batches the keys resolved in the same tick and caches within its
        # own lifetime. A loader that outlived the request would be a cache
        # with no invalidation, serving one request's projects to the next.
        self.project_loader = build_project_loader(project_service)
        self.project_milestones_loader = build_project_milestones_loader(
            project_service
        )

        # Teams and workspaces as entities, for the resolvers that ask about
        # them rather than about this request's scope. Shared instances, not
        # second copies: one request gets one of each.
        self.team_service = team_service
        self.workspace_service = workspace_service

        # Built here rather than taken as an argument, which is the one
        # place in this class where construction beats injection. A
        # DataLoader's cache must live exactly as long as the request, and
        # this object is the request; a loader passed in is a loader whose
        # lifetime is the caller's business, and a caller that built one per
        # process -- or reused one across two requests -- would serve the
        # second client the first client's cycles from cache, with nothing
        # here able to tell. Over the same service the resolvers use, so a
        # cycle read through `Issue.cycle` and one read through `cycle(id:)`
        # cannot come from two differently-configured paths.
        self.cycle_loader = CycleLoader(cycle_service)
        # Required for the same reason `tenant` is, and worth saying
        # separately because it is the one a caller is most likely to think
        # optional: `Issue.parent`, `Issue.children` and `Issue.relations`
        # hang off a type every issue query already selects, so a context
        # built without this fails on an ordinary query rather than only on
        # the relation mutations -- and it fails as an AttributeError inside
        # a resolver, which the schema masks as "Internal server error".
        self.relation_service = relation_service

        # There is deliberately no workspace on this object. A per-request
        # "current tenant" is what `app/graphql/tenancy.py` used to hold, and
        # the reason it is gone is not that the constant in it was temporary:
        # a scope living on the context is a scope a resolver can reach
        # without having been given one, so a resolver that forgot to
        # authorize would still find a workspace to work in. The scope now
        # arrives per FIELD, from `app.graphql.scope.authorized_scope`, which
        # is the only thing that produces one -- and a document may legally
        # name two workspaces in two root fields, which no single ambient
        # value could have served.

        # Carried because the Set-Cookie policy depends on it: Secure is only
        # legal where the deployment speaks https. Passed in rather than read
        # from settings at the point of use, so that one request cannot end
        # up writing a cookie under one environment's rules and clearing it
        # under another's.
        self.environment = environment

        self._viewer: asyncio.Future[UserEntity | None] | None = None

    @functools.cached_property
    def issue_labels(self) -> DataLoader[IssueLabelKey, list[LabelEntity]]:
        """Batches `Issue.labels` across whatever page of issues asked for it.

        Per request and never wider, which is what makes a cache keyed by
        (workspace, issue) safe to hold at all: a loader living longer than one
        request would go on answering with labels that have since changed, and
        one shared between requests would answer one caller with another's
        batch.

        Built on first use rather than in `__init__`. `DataLoader` binds itself
        to the running event loop, and a context is constructed in places where
        there need not be one -- `tests/conftest.py::graphql_context` is called
        from synchronous test bodies. Deferring it also means a document that
        never selects `labels` never builds one.
        """
        return issue_label_loader(self.label_service)

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

    # Constructed once and shared by every resolver that needs one. Two
    # instances would be two objects answering the same question over the
    # same pool, and any caching either one grows later would then be per
    # copy rather than per request.
    workspace_service = WorkspaceService(pool=pool, repository=WorkspaceRepository())
    team_service = TeamService(pool=pool, repository=TeamRepository())

    return VectorContext(
        issue_service=IssueService(
            pool=pool,
            repository=IssueRepository(),
            # Creating an issue allocates a number off the team's counter and
            # resolves the state it starts in, both inside the issue
            # service's own transaction. Same instance as below: one request
            # gets one team service.
            teams=team_service,
        ),
        membership_service=MembershipService(
            pool=pool,
            repository=MembershipRepository(),
        ),
        project_service=ProjectService(
            pool=pool,
            repository=ProjectRepository(),
            # Deleting a project detaches the issues pointing at it, in the
            # same transaction. SQL against `issues` belongs to the repository
            # that owns that table, so the service reaches across to it rather
            # than the project repository growing statements about issues.
            issue_repository=IssueRepository(),
        ),
        auth_service=AuthService(
            pool=pool,
            users=UserRepository(),
            sessions=SessionRepository(),
            hasher=Argon2PasswordHasher(),
        ),
        label_service=LabelService(
            pool=pool,
            repository=LabelRepository(),
            # One service owns both tables, because applying a label is one
            # operation over two of them: the join row is meaningless without
            # the label, and the per-issue cap is a rule about the pair.
            issue_label_repository=IssueLabelRepository(),
        ),
        comment_service=CommentService(pool=pool, repository=CommentRepository()),
        cycle_service=CycleService(pool=pool, repository=CycleRepository()),
        team_service=team_service,
        workspace_service=workspace_service,
        relation_service=RelationService(
            pool=pool,
            repository=RelationRepository(),
        ),
        environment=environment,
    )
