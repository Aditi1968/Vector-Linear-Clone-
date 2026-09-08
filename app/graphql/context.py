import asyncio
import functools

from strawberry.dataloader import DataLoader
from strawberry.fastapi import BaseContext

from app.config import Environment, get_settings
from app.db import get_pool
from app.domain.auth import UserEntity
from app.domain.issues import IssueEntity
from app.domain.labels import LabelEntity
from app.graphql.loaders.cycles import CycleLoader
from app.graphql.loaders.documents import (
    build_document_content_loader,
    build_revision_content_loader,
)
from app.graphql.loaders.initiatives import build_initiative_updates_loader
from app.graphql.loaders.issues import IssueKey, build_issue_loader
from app.graphql.loaders.labels import IssueLabelKey, issue_label_loader
from app.graphql.loaders.projects import (
    build_project_dependencies_loader,
    build_project_loader,
    build_project_milestones_loader,
    build_project_updates_loader,
)
from app.http_cookies import read_session_token
from app.repositories.activity import ActivityRepository
from app.repositories.bulk import BulkRepository
from app.repositories.comments import CommentRepository
from app.repositories.cycles import CycleRepository
from app.repositories.documents import DocumentRepository
from app.repositories.embedding_jobs import EmbeddingJobRepository
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.github import GithubRepository
from app.repositories.initiatives import InitiativeRepository
from app.repositories.invitations import InvitationRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.issues import IssueRepository
from app.repositories.label_groups import LabelGroupRepository
from app.repositories.labels import LabelRepository
from app.repositories.memberships import MembershipRepository
from app.repositories.notifications import NotificationRepository
from app.repositories.projects import ProjectRepository
from app.repositories.recurrences import RecurrenceRepository
from app.repositories.relations import RelationRepository
from app.repositories.releases import ReleaseRepository
from app.repositories.saved_views import FavoriteRepository, SavedViewRepository
from app.repositories.sessions import SessionRepository
from app.repositories.slack import SlackRepository
from app.repositories.subscribers import SubscriberRepository
from app.repositories.teams import TeamRepository
from app.repositories.templates import TemplateRepository
from app.repositories.triage import TriageRepository
from app.repositories.users import UserRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.activity import ActivityService
from app.services.auth import AuthService
from app.services.bulk import BulkService
from app.services.comments import CommentService
from app.services.cycles import CycleService
from app.services.documents import DocumentService
from app.services.embeddings import load_embedder
from app.services.github import GithubAppConfig, GithubService
from app.services.initiatives import InitiativeService
from app.services.issues import IssueService
from app.services.labels import LabelService
from app.services.memberships import MembershipService
from app.services.passwords import Argon2PasswordHasher
from app.services.projects import ProjectService
from app.services.relations import RelationService
from app.services.releases import ReleaseService
from app.services.saved_views import FavoriteService, SavedViewService
from app.services.search import SearchService
from app.services.slack import DatabaseTokenStore, SlackService, SlackWebClient
from app.services.teams import TeamService
from app.services.templates import TemplateService
from app.services.triage import TriageService


class VectorContext(BaseContext):
    """Per-request GraphQL context."""

    def __init__(
        self,
        issue_service: IssueService,
        auth_service: AuthService,
        team_service: TeamService,
        membership_service: MembershipService,
        label_service: LabelService,
        comment_service: CommentService,
        cycle_service: CycleService,
        project_service: ProjectService,
        initiative_service: InitiativeService,
        document_service: DocumentService,
        relation_service: RelationService,
        release_service: ReleaseService,
        saved_view_service: SavedViewService,
        favorite_service: FavoriteService,
        search_service: SearchService,
        github_service: GithubService,
        slack_service: SlackService,
        activity_service: ActivityService,
        template_service: TemplateService,
        triage_service: TriageService,
        bulk_service: BulkService,
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
        self.initiative_service = initiative_service

        # Documents, their history and their discussion. One service over
        # all three tables, because a version snapshot is not a separate
        # operation from the edit that caused it -- they are two writes
        # inside one transaction, and a second service holding the
        # revisions would be a second connection with no way to be in it.
        self.document_service = document_service
        self.search_service = search_service

        # What shipped, where, and the notes that say so. Holds the whole
        # feature rather than sharing GithubService's: a release READS
        # migration 017's tables and never writes them, so the two services
        # have no state to keep in step -- and putting release creation behind
        # the object that also applies webhooks would give a webhook path a
        # method that cuts releases.
        self.release_service = release_service

        # Saved views and favorites. Two services over two tables rather
        # than one over both: a favourite points at a team, a project or a
        # saved view, and only the third has anything to do with saved
        # views. What couples them is one statement -- deleting a view
        # clears the favourites pointing at it -- which is a service
        # reaching across to a second repository inside one transaction.
        self.saved_view_service = saved_view_service
        self.favorite_service = favorite_service

        # Holds the deployment's GitHub App credentials, and is the reason
        # nothing else in this context does. The service answers `configured`
        # and never the values behind it, and no Strawberry type is built from
        # `GithubAppConfig`, so there is no field a client can select a
        # credential through.
        self.github_service = github_service

        # Reads a status and removes an installation, and does neither without
        # an AuthorizedWorkspaceScope. The connect half of the integration is
        # not reachable from here at all: an OAuth grant arrives through
        # app/rest/slack.py, which builds its own instance of this service
        # over the same pool.
        self.slack_service = slack_service

        # Activity, notifications and subscriptions. Every WRITE reachable
        # through this object is one a person performs on their own row --
        # marking an item read, watching or unwatching an issue -- and each
        # holds a transaction of its own. Nothing here records that something
        # happened TO an issue: an activity row, an inbox item and an
        # auto-subscribe are written by the service that causes them, on that
        # service's own connection and inside its transaction, through the
        # module-level functions in app/services/activity.py. A service here
        # that could do that would be a way to record history for a change
        # that had not happened yet -- or that was about to be rolled back.
        self.activity_service = activity_service

        # Reads templates and files issues from them, which is why it holds
        # the issue and label services rather than their repositories: every
        # rule an apply has to respect -- which state a new issue starts in,
        # which number it gets, how many labels one may wear -- already lives
        # in a service, and reaching past them would grow a second
        # `issueCreate` nobody would think to keep in step.
        self.template_service = template_service

        # The triage queue and the multi-issue writes, each behind its own
        # service. Both are required rather than defaulted, for the reason
        # every other slot here is: a context built without one fails on the
        # first request that reaches the field rather than at construction, and
        # every test that never touches triage would go on passing.
        self.triage_service = triage_service

        # Nothing on this object is a way to mutate many issues without an
        # authorized scope. `BulkService` takes a WorkspaceScope on every
        # method, exactly as `IssueService` does, and the ids it is handed are
        # checked against that scope and against nothing else.
        self.bulk_service = bulk_service

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
        self.project_updates_loader = build_project_updates_loader(project_service)
        self.project_dependencies_loader = build_project_dependencies_loader(
            project_service
        )
        self.initiative_updates_loader = build_initiative_updates_loader(
            initiative_service
        )

        # `Document.content` and `DocumentRevision.content`. Loaders rather
        # than plain fields because a body may be 200,000 characters, so a
        # page of documents carrying them inline would be megabytes for a
        # screen that renders titles -- see app/graphql/types/document.py.
        self.document_content_loader = build_document_content_loader(document_service)
        self.revision_content_loader = build_revision_content_loader(document_service)

        # Teams as entities, for the resolvers that ask about them rather
        # than about this request's scope.
        #
        # WorkspaceService is deliberately NOT published here. Its
        # `scope_for_slug` resolves a slug to a bare `WorkspaceScope` --
        # identity, with no membership checked -- and a `WorkspaceScope`
        # satisfies every signature that takes one, including the ones meant
        # to require an `AuthorizedWorkspaceScope`. Reachable from the
        # context, it is a way for a future resolver to obtain a tenant
        # without authorizing it, and for the type system to say nothing.
        # Workspaces are resolved through `app.graphql.scope.authorized_scope`
        # and nowhere else.
        self.team_service = team_service

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

    @functools.cached_property
    def issue_summaries(self) -> DataLoader[IssueKey, IssueEntity | None]:
        """Batches an issue-per-row field -- `Notification.issue` today.

        Per request and never wider, for the reason `issue_labels` above
        gives: a loader living longer than one request is a cache with no
        invalidation, and one shared between requests answers one caller with
        another's batch.

        Built on first use rather than in `__init__`, also for that field's
        reason. `DataLoader` binds itself to the running event loop and a
        context is constructed where there need not be one, and a document
        that never selects the field never builds a loader.
        """
        return build_issue_loader(self.issue_service)

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
    settings = get_settings()
    environment = settings.environment

    # One per request, shared by every resolver that needs one. Two instances
    # would be two objects answering the same question over the same pool, and
    # any caching either one grows later would then be per copy.
    team_service = TeamService(pool=pool, repository=TeamRepository())

    # Named rather than built inline, because `TemplateService` needs both:
    # applying a template files an issue and puts labels on it, through the
    # services that own those rules. One instance each per request, for the
    # reason `team_service` above is one -- two would be two objects answering
    # the same question over the same pool, and any caching either grows later
    # would then be per copy.
    issue_service = IssueService(
        pool=pool,
        repository=IssueRepository(),
        # Creating an issue allocates a number off the team's counter and
        # resolves the state it starts in, both inside the issue service's own
        # transaction. Same instance as below: one request gets one team
        # service.
        teams=team_service,
    )

    label_service = LabelService(
        pool=pool,
        repository=LabelRepository(),
        # One service owns all three tables, because applying a label is one
        # operation over two of them -- the join row is meaningless without
        # the label, and the per-issue cap is a rule about the pair -- and
        # because a label's group decides whether the join row is allowed at
        # all. Deleting a group also ungroups its labels in one transaction,
        # which a separate service could not hold.
        issue_label_repository=IssueLabelRepository(),
        group_repository=LabelGroupRepository(),
    )

    return VectorContext(
        issue_service=issue_service,
        membership_service=MembershipService(
            pool=pool,
            repository=MembershipRepository(),
            # Creating a workspace writes the workspace and its owner's
            # membership in one transaction, and accepting an invitation
            # writes the acceptance and the membership it grants in another.
            # SQL against each table still belongs to the repository that
            # owns it; the service is what holds the boundary they are
            # written inside.
            workspaces=WorkspaceRepository(),
            invitations=InvitationRepository(),
        ),
        project_service=ProjectService(
            pool=pool,
            repository=ProjectRepository(),
            # Deleting a project detaches the issues pointing at it, in the
            # same transaction. SQL against `issues` belongs to the repository
            # that owns that table, so the service reaches across to it rather
            # than the project repository growing statements about issues.
            issue_repository=IssueRepository(),
            # And the initiatives it belongs to, for the same reason:
            # `initiative_projects_project_fk` is RESTRICT, so those rows go
            # first, and the SQL against that table belongs to the repository
            # that owns it.
            initiative_repository=InitiativeRepository(),
        ),
        initiative_service=InitiativeService(
            pool=pool,
            repository=InitiativeRepository(),
        ),
        document_service=DocumentService(
            pool=pool,
            repository=DocumentRepository(),
        ),
        auth_service=AuthService(
            pool=pool,
            users=UserRepository(),
            sessions=SessionRepository(),
            hasher=Argon2PasswordHasher(),
        ),
        label_service=label_service,
        comment_service=CommentService(pool=pool, repository=CommentRepository()),
        cycle_service=CycleService(pool=pool, repository=CycleRepository()),
        team_service=team_service,
        relation_service=RelationService(
            pool=pool,
            repository=RelationRepository(),
        ),
        release_service=ReleaseService(
            pool=pool,
            # One repository, even though the range it resolves reads
            # `github_commits` and `github_pull_requests`. Those two statements
            # are SELECTs in service of the releases feature and belong with
            # the rest of its SQL; reaching into GithubRepository for them
            # would mean that class growing methods about release windows,
            # which is not what it owns.
            repository=ReleaseRepository(),
        ),
        saved_view_service=SavedViewService(
            pool=pool,
            repository=SavedViewRepository(),
            # Deleting a view drops the favourites pointing at it, in the
            # same transaction. SQL against `favorites` belongs to the
            # repository that owns that table, so the service reaches across
            # to it rather than the saved-view repository growing statements
            # about favourites.
            favorites=FavoriteRepository(),
        ),
        favorite_service=FavoriteService(
            pool=pool,
            # A fresh instance rather than the one above. A repository here
            # holds no state and no connection -- it is a namespace for
            # statements -- so there is nothing for one request to get two of.
            repository=FavoriteRepository(),
        ),
        search_service=SearchService(
            pool=pool,
            # Three repositories, because search reads three tables and the
            # SQL for a table belongs to the repository that owns it. Fresh
            # instances rather than shared ones: a repository here holds no
            # state and no connection -- it is a namespace for statements --
            # so there is nothing for one request to get two of.
            issue_repository=IssueRepository(),
            project_repository=ProjectRepository(),
            embedding_repository=EmbeddingRepository(),
            # The fourth, and it reads a table no search statement touches:
            # `embeddingIndexingState` answers "is the index still building"
            # from `embedding_jobs`, which is the background worker's own
            # bookkeeping. Wired here rather than left to the worker, because
            # the question is asked by a client on the request path and the
            # worker is a process-wide object with no scope.
            job_repository=EmbeddingJobRepository(),
            # What makes search hybrid rather than lexical, decided HERE and
            # not per request inside the service. `load_embedder` never fails
            # -- it falls back to a deterministic local embedder when no model
            # library is installed -- so this wire is always live; a deployment
            # that wants lexical-only search passes None here, which is the one
            # place that decision belongs. See SearchService's docstring on why
            # degradation is a wire and not a try/except.
            #
            # Process-wide, not per request: `load_embedder` is cached, so a
            # real model is loaded from disk once rather than on every GraphQL
            # call this function serves.
            embedder=load_embedder(),
        ),
        github_service=GithubService(
            pool=pool,
            repository=GithubRepository(),
            # Built from the same settings this function already resolved, so
            # the GraphQL layer and app/rest/github.py cannot disagree about
            # whether this deployment has a GitHub App.
            config=GithubAppConfig.from_settings(settings),
        ),
        slack_service=SlackService(
            pool=pool,
            repository=SlackRepository(),
            token_store=DatabaseTokenStore(),
            # Whether this deployment has a Slack app at all, resolved from
            # settings here rather than read inside the service, so that one
            # request cannot answer one field as configured and another as
            # not. `settings` is already resolved above for `environment`.
            configured=settings.slack_configured,
            # The two Web API calls the channel and notification resolvers
            # make. Stateless and credential-free: the bot token is passed per
            # call, so this object holds nothing worth printing. Built here
            # and not in app/rest/slack.py, because no REST route talks to the
            # Web API -- the OAuth callback only exchanges a code.
            web=SlackWebClient(),
        ),
        # One service over both tables, because a history row and an inbox
        # item are two records of one moment: the event that happened, and
        # who has to look at it. They stay two TABLES and two entities --
        # that distinction is the point of migration 012 -- but a reader
        # asking "what happened here, and does it concern me" is asking one
        # question.
        activity_service=ActivityService(
            pool=pool,
            repository=ActivityRepository(),
            notifications=NotificationRepository(),
            # The third table of the same question. A subscription is the
            # standing answer to "whose problem is this", which the
            # notification half asks on every write, so the two are read
            # together by every screen that shows an issue.
            subscribers=SubscriberRepository(),
        ),
        template_service=TemplateService(
            pool=pool,
            repository=TemplateRepository(),
            # The schedule a template files itself on. A second repository
            # rather than statements about `issue_recurrences` inside
            # `TemplateRepository`: a recurrence has no life without the
            # template it names, so it belongs to this service -- but the SQL
            # for a table belongs to the repository that owns that table. Same
            # split as `SavedViewService` and `FavoriteRepository`.
            recurrences=RecurrenceRepository(),
            # Services, not repositories: applying a template must go through
            # the same rules `issueCreate` and `issueLabelAttach` enforce, and
            # through the same composite foreign keys, so a stored id is
            # re-checked against the applying caller's workspace rather than
            # trusted because it was checked once when it was saved.
            issues=issue_service,
            labels=label_service,
        ),
        triage_service=TriageService(
            pool=pool,
            repository=TriageRepository(),
            # Marking a duplicate writes an `issue_relations` row in the same
            # transaction as the decline, and SQL against that table belongs to
            # the repository that owns it. A fresh instance rather than the one
            # RelationService holds: a repository here carries no state and no
            # connection -- it is a namespace for statements -- so there is
            # nothing for one request to get two of.
            relations=RelationRepository(),
            # Changing a queued issue's team allocates a number off the TARGET
            # team's counter and resolves the state it arrives in, both inside
            # the triage service's own transaction. Same instance as the issue
            # service's: one request gets one team service.
            teams=team_service,
        ),
        bulk_service=BulkService(
            pool=pool,
            repository=BulkRepository(),
            # Bulk label changes write `issue_labels`, so the service reaches
            # across to the repository that owns that table rather than
            # BulkRepository growing statements about it.
            issue_labels=IssueLabelRepository(),
        ),
        environment=environment,
    )
