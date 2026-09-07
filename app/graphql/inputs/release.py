from uuid import UUID

import strawberry

from app.graphql.types.release import EnvironmentKindType, ReleaseStatusType


@strawberry.input
class EnvironmentCreateInput:
    # A slug and not a workspace id, deliberately. CLAUDE.md forbids trusting a
    # workspace id from the frontend: the slug is a public string that selects
    # WHAT is being asked about, and `app.graphql.scope` decides whether the
    # session behind the request may act there.
    workspace_slug: str

    name: str

    # No default. `environments_kind_check` names the legal kinds and refuses
    # everything else, but nothing should choose one on a caller's behalf --
    # guessing 'custom' would file a real production environment as a sandbox,
    # and guessing 'development' would do the reverse.
    kind: EnvironmentKindType


@strawberry.input
class ReleaseCreateInput:
    """Cut a release: what shipped, from where, to where."""

    workspace_slug: str

    name: str
    environment_id: UUID

    # GitHub's numeric id, as an ID rather than an Int: it is a BIGINT and a
    # GraphQL Int is a signed 32-bit value, so the wire type has to be a string
    # -- the same choice `Release.repositoryId` and the GitHub types make.
    repository_id: strawberry.ID

    # The commit being released. Validated for shape here and then resolved
    # against `github_commits` inside this workspace: a SHA naming another
    # tenant's commit is *not found*, which is the same answer a SHA that
    # exists nowhere gets.
    commit_sha: str

    # Where the release range starts, and it has three meanings.
    #
    # `strawberry.UNSET` as the default is what makes an omitted field arrive
    # as UNSET rather than as None, and here the distinction is the feature
    # rather than a nicety:
    #
    # * omitted -- start from wherever the last deploy of this repository into
    #   this environment left off. This is what a pipeline sends, because it is
    #   the value a pipeline would otherwise have to remember client-side.
    # * null -- no lower bound. The first release into an environment.
    # * a SHA -- exactly this range.
    #
    # A `| None` with no UNSET default could not express the first at all: it
    # would make "I did not say" and "start from the beginning" the same
    # request, and every routine deploy would re-list the entire history.
    previous_commit_sha: str | None = strawberry.UNSET


@strawberry.input
class ReleaseStatusSetInput:
    """Move a release along its lifecycle.

    One mutation with a target status rather than `releaseDeploy` /
    `releaseFail` / `releaseRollBack`, because the three would be three
    resolvers differing only in a constant, over one service method, with one
    set of refusals -- and a fourth status later would need a fourth mutation
    rather than a new enum member clients already know how to send.

    Which moves are legal is not expressible in this type and deliberately is
    not attempted: it depends on the release's current status, which the schema
    cannot see. `app.domain.releases.RELEASE_TRANSITIONS` holds the rule and an
    illegal move comes back as an INVALID_TRANSITION field error naming the
    state the release is actually in.
    """

    workspace_slug: str
    id: UUID
    status: ReleaseStatusType


@strawberry.input
class ReleaseDeleteInput:
    workspace_slug: str
    id: UUID
