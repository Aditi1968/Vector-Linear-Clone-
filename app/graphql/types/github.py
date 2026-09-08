from datetime import datetime
from enum import Enum
from uuid import UUID

import strawberry

from app.domain.github import (
    DEVELOPMENT_LIMIT,
    GithubAutomationEntity,
    GithubCommitEntity,
    GithubDevelopmentEntity,
    GithubIntegrationEntity,
    GithubPullRequestEntity,
)
from app.graphql.types.errors import ValidationErrorType


# What a Development panel shows without asking for more. Ten of each is what
# fits beside an issue; the service has already capped the read at
# DEVELOPMENT_LIMIT, so this is a display size rather than a bound on work.
DEFAULT_DEVELOPMENT_FIRST = 10


def _bounded(first: int) -> int:
    """A slice length inside the service's own cap.

    Clamped rather than validated, because there is nothing to refuse: the
    lists are already in memory and already capped, so a negative or enormous
    `first` is a client mistake with an obvious right answer rather than a
    request the server cannot serve. A negative would otherwise slice from the
    END, which would silently answer with the oldest activity.
    """
    return max(0, min(first, DEVELOPMENT_LIMIT))


@strawberry.enum(name="GithubIntegrationStatus")
class GithubIntegrationStatusType(Enum):
    """The transport's copy of app.domain.github.GITHUB_STATUSES.

    Four members rather than a `connected: Boolean`, because a client has to
    render four different things: an operator's notice for a deployment with
    no GitHub App, a Connect button for a workspace that has not installed it,
    a wait for a workspace whose claim GitHub has not answered for, and the
    installation itself. A boolean would make the first two identical and put
    a button in front of a user whose click cannot work.

    PENDING arrived with migrations/016_github_installation_trust.sql and is
    not cosmetic. A client that renders it as CONNECTED is reporting an
    installation *claim* as an established fact, which is the defect that
    migration closes -- so every consumer grows a branch for it rather than
    falling through to whichever one its `else` happens to be.

    tests/test_github.py pins these members equal to the domain tuple.
    """

    UNCONFIGURED = "unconfigured"
    DISCONNECTED = "disconnected"
    PENDING = "pending"
    CONNECTED = "connected"


@strawberry.type(name="GithubRepository")
class GithubRepositoryType:
    """One repository the installation covers.

    `repositoryId` is an ID rather than an Int deliberately. GraphQL's Int is
    signed 32-bit, and GitHub's repository ids are an opaque counter it owns
    and grows -- a value past 2^31 would be a serialisation error on a field
    that had worked for years. ID serialises as a string and carries no
    arithmetic promise, which is the honest description of an identifier from
    somebody else's system.
    """

    repository_id: strawberry.ID
    full_name: str
    tracked: bool = strawberry.field(
        description=(
            "Whether this workspace applies GitHub deliveries about this "
            "repository. False is a choice an admin made here, not something "
            "GitHub said: the installation still covers it, and Vector is "
            "declining the pull requests and pushes. Development history "
            "already collected is kept and still shown."
        )
    )


@strawberry.type(
    name="GithubIssueAutomation",
    description=(
        "What a pull request does to one team's issues. Present only for "
        "teams that have turned it on; an absent team is a team with no "
        "automation, which is the default."
    ),
)
class GithubIssueAutomationType:
    """One team's status automation.

    Both state fields are nullable and null means that half does nothing --
    a team may automate the merge and leave starting to whoever is doing the
    work. There is no `enabled` field, because there is no such column: an
    automation that is off has no row and therefore no entry in this list.
    See migration 030.

    The states are returned as ids rather than as `WorkflowState` objects.
    A client rendering this screen is already reading `teams { workflowStates
    { id name category } }` to offer the choice, so an embedded copy would be
    the same rows fetched twice and two places for the name to be stale.
    """

    team_id: UUID
    started_state_id: UUID | None = strawberry.field(
        description=(
            "Where an issue goes when a pull request naming it opens, "
            "reopens, or is marked ready for review. A pull request opened "
            "as a DRAFT moves nothing -- a draft says the work is not ready."
        )
    )
    completed_state_id: UUID | None = strawberry.field(
        description=(
            "Where an issue goes when a pull request naming it MERGES. A "
            "pull request closed without merging moves nothing: an abandoned "
            "attempt is not shipped work."
        )
    )

    @classmethod
    def from_entity(
        cls,
        entity: GithubAutomationEntity,
    ) -> "GithubIssueAutomationType":
        return cls(
            team_id=entity.team_id,
            started_state_id=entity.started_state_id,
            completed_state_id=entity.completed_state_id,
        )


@strawberry.type(name="GithubIntegration")
class GithubIntegrationType:
    """A workspace's GitHub integration, as an admin of it sees it.

    Every field here is a fact about *what* is connected. None of them is a
    credential, and none can become one by accident: there is no token field,
    no secret field, no signing material, and the entity behind this type is
    built from a table that has no such column -- so adding one would take a
    migration, a repository change and a resolver change rather than a typo.

    `accountLogin` is nullable because the account is not known at the instant
    the installation is recorded; the setup redirect does not carry it, and
    the first signed webhook does. `connectedAt` and `connectedById` are
    nullable for the plainer reason that there may be no installation at all.
    """

    status: GithubIntegrationStatusType
    account_login: str | None
    connected_at: datetime | None
    connected_by_id: UUID | None
    repositories: list[GithubRepositoryType]

    # The per-team status automations, for the teams that have one. Empty means
    # no team in this workspace has turned one on, which is every workspace
    # until somebody does. Unlike `repositories` this survives disconnecting:
    # an automation is configuration a team wrote about its own board.
    #
    # Documented in a comment rather than a `description=`, because
    # `tests/test_github.py` pins this type's field list by parsing the printed
    # SDL a line at a time -- a docblock above a field would read as three more
    # fields. The type's own description carries what a client needs.
    automations: list[GithubIssueAutomationType]

    @classmethod
    def from_entity(cls, entity: GithubIntegrationEntity) -> "GithubIntegrationType":
        """Build the transport type, raising on a status this schema cannot say.

        `GithubIntegrationStatusType(entity.status)` raises ValueError for a
        status the enum does not carry, which reaches the client as a masked
        internal error and the logs as a real traceback. That is the intended
        outcome, for the reason WorkspaceMembershipType.from_entity gives: a
        fallback would report a state the server does not believe.
        """
        installation = entity.installation

        return cls(
            status=GithubIntegrationStatusType(entity.status),
            account_login=None if installation is None else installation.account_login,
            connected_at=None if installation is None else installation.connected_at,
            connected_by_id=(
                None if installation is None else installation.connected_by
            ),
            repositories=[
                GithubRepositoryType(
                    repository_id=strawberry.ID(str(repository.repository_id)),
                    full_name=repository.full_name,
                    tracked=repository.tracked,
                )
                for repository in entity.repositories
            ],
            automations=[
                GithubIssueAutomationType.from_entity(automation)
                for automation in entity.automations
            ],
        )


@strawberry.type(name="GithubIntegrationPayload")
class GithubIntegrationPayload:
    """The result of a settings write, with the field errors it can produce.

    A payload where `githubDisconnect` answers the integration bare, and the
    difference is what can go wrong. Disconnecting has no correctable input --
    there is no field a client could fix to be allowed -- so an errors list
    there would always be empty. These two take input an admin can get wrong:
    a state belonging to another team, a team with no state to derive a
    default from, a repository a release still names. Each of those has a
    field to point at, and pointing at it is the difference between a form
    that highlights a select and a toast saying something failed.
    """

    integration: GithubIntegrationType | None
    errors: list[ValidationErrorType]


@strawberry.enum(name="GithubPullRequestState")
class GithubPullRequestStateType(Enum):
    """What a Development section shows for a pull request.

    Four members and none of them a column. GitHub has no such field: it has
    `state` (open or closed), a `draft` boolean and a `merged_at` instant, and
    migrations/017_github_development.sql stores all three rather than
    flattening them. This is the flattening, derived in
    `app.domain.github.pull_request_display_state`, where a precedence change
    is a code change with a test rather than a migration.

    MERGED and CLOSED are separate because they are the whole question a
    reader has about a closed pull request -- shipped, or abandoned -- and
    `state` alone cannot answer it.
    """

    DRAFT = "draft"
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


@strawberry.enum(name="GithubLinkSource")
class GithubLinkSourceType(Enum):
    """Where the identifier that made a link was written.

    The transport's copy of `app.domain.github.LINK_SOURCES` and of
    github_pull_request_issues_source_check; tests/test_github_development.py
    pins the three equal.

    Published rather than kept internal because the three are not equally
    strong evidence and a reader is entitled to weigh them: a branch name is
    chosen by someone with push access, while a title on a public repository
    is written by whoever opened the pull request. A client that wants to
    render "linked from branch" differently from "mentioned in the title" has
    the fact it needs.
    """

    TITLE = "title"
    BODY = "body"
    BRANCH = "branch"


@strawberry.type(name="GithubPullRequest")
class GithubPullRequestType:
    """One pull request attached to an issue.

    `repositoryId` is an ID rather than an Int for the reason
    `GithubRepository.repositoryId` is: GraphQL's Int is signed 32-bit and
    GitHub's ids are a counter it owns and grows.

    `number` stays an Int. It is a per-repository counter starting at 1, and
    a repository with two billion pull requests is not a case worth widening
    the type for.
    """

    repository_id: strawberry.ID
    repository: str = strawberry.field(
        description="The repository this pull request is on, as owner/name."
    )
    number: int
    title: str
    state: GithubPullRequestStateType
    branch: str | None = strawberry.field(
        description=(
            "The branch the pull request is from, or null where the payload "
            "omitted it -- a deleted or cross-fork head."
        )
    )
    url: str | None = strawberry.field(
        description=(
            "The link GitHub reported, stored rather than rebuilt from the "
            "owner, name and number: GitHub owns its URL layout, and a "
            "rebuilt link is a guess that breaks silently."
        )
    )
    merged_at: datetime | None
    updated_at: datetime | None = strawberry.field(
        description=(
            "GitHub's own updated_at, not Vector's. Null where the payload "
            "carried none."
        )
    )
    linked_by: list[GithubLinkSourceType] = strawberry.field(
        description=(
            "Which of the pull request's title, body and branch name this "
            "issue's identifier appears in. More than one is ordinary, and "
            "each is retracted independently: editing the title removes the "
            "title's link and leaves the branch's."
        )
    )

    @classmethod
    def from_entity(
        cls,
        entity: GithubPullRequestEntity,
    ) -> "GithubPullRequestType":
        return cls(
            repository_id=strawberry.ID(str(entity.repository_id)),
            repository=entity.repository_full_name,
            number=entity.number,
            title=entity.title,
            state=GithubPullRequestStateType(entity.display_state),
            branch=entity.head_ref,
            url=entity.url,
            merged_at=entity.merged_at,
            updated_at=entity.github_updated_at,
            linked_by=[GithubLinkSourceType(source) for source in entity.link_sources],
        )


@strawberry.type(name="GithubCommit")
class GithubCommitType:
    """One commit attached to an issue."""

    repository_id: strawberry.ID
    repository: str
    sha: str = strawberry.field(
        description="The full 40-character SHA, which is the commit's identity."
    )
    short_sha: str = strawberry.field(
        description=(
            "The first seven characters, which is what a list shows. A "
            "rendering and never an identity: the abbreviation is ambiguous "
            "by construction."
        )
    )
    message: str = strawberry.field(description="The whole commit message.")
    summary: str = strawberry.field(
        description="The message's first line, which is what a list shows."
    )
    url: str | None
    committed_at: datetime | None

    @classmethod
    def from_entity(cls, entity: GithubCommitEntity) -> "GithubCommitType":
        return cls(
            repository_id=strawberry.ID(str(entity.repository_id)),
            repository=entity.repository_full_name,
            sha=entity.sha,
            short_sha=entity.short_sha,
            message=entity.message,
            summary=entity.summary,
            url=entity.url,
            committed_at=entity.committed_at,
        )


@strawberry.type(name="GithubDevelopment")
class GithubDevelopmentType:
    """One issue's development activity, and the branch to start it with.

    Non-null everywhere, including for an issue with nothing attached: an
    empty Development section is a real state with a real thing to render --
    the suggested branch name -- and a nullable field here would make a client
    infer from an absence the thing this type exists to say.

    Both lists are capped rather than paged, at
    `app.services.github.DEVELOPMENT_LIMIT`. This is a short list beside an
    issue and not a feed; a cursor would be API surface for a scroll nobody
    performs.

    They still declare `first`, and that is not the cursor coming back in.
    `app.graphql.limits` prices a composite field as `page_size *
    inner_complexity` and reads that page size from the argument -- so a list
    field with no `first` is charged as though it returned ONE row, which is
    how a document selecting this under a hundred issues measures as cheap
    while returning five thousand pull requests. The argument is what makes
    the budget see the fan-out. Slicing it costs nothing: both lists are
    already resolved and in memory.
    """

    branch_name: str = strawberry.field(
        description=(
            "A deterministic branch name for this issue -- "
            "eng-142-fix-slack-oauth-callback. Safe to paste into "
            "`git checkout -b` for any title: unicode is folded, punctuation "
            "collapses, and a title that folds to nothing leaves the "
            "identifier alone. Creating it needs no GitHub permission and "
            "this field creates nothing; the identifier leads so that a pull "
            "request opened from the branch links back without anyone typing "
            "the identifier twice."
        )
    )

    # Carried, not exposed. The resolvers below slice these; publishing them
    # as well would be a second, unpriced spelling of the same two lists.
    all_pull_requests: strawberry.Private[list[GithubPullRequestType]]
    all_commits: strawberry.Private[list[GithubCommitType]]

    @strawberry.field
    def pull_requests(
        self,
        first: int = DEFAULT_DEVELOPMENT_FIRST,
    ) -> list[GithubPullRequestType]:
        """The pull requests linked to this issue, newest activity first."""
        return self.all_pull_requests[: _bounded(first)]

    @strawberry.field
    def commits(self, first: int = DEFAULT_DEVELOPMENT_FIRST) -> list[GithubCommitType]:
        """The commits linked to this issue, newest first."""
        return self.all_commits[: _bounded(first)]

    @classmethod
    def from_entity(
        cls,
        entity: GithubDevelopmentEntity,
    ) -> "GithubDevelopmentType":
        return cls(
            branch_name=entity.branch_name,
            all_pull_requests=[
                GithubPullRequestType.from_entity(pull) for pull in entity.pull_requests
            ],
            all_commits=[
                GithubCommitType.from_entity(commit) for commit in entity.commits
            ],
        )
