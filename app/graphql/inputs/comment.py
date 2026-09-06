from uuid import UUID

import strawberry


@strawberry.input
class CommentCreateInput:
    """A new comment on an issue.

    There is deliberately no `authorId`, and there never may be. Authorship is
    not the client's to assert: a field here would be an impersonation API, and
    one that would be hard to remove once anything had generated types against
    it. The author is `info.context.viewer()` -- the session the request
    actually presented -- and the database will not accept any other, because
    `comments_author_fk` is composite against this workspace's own membership.
    """

    # Every workspace-scoped mutation names its tenant, and every mutation
    # that takes an `input` names it HERE rather than beside the input. One
    # place per operation, so a client never has to remember which mutations
    # spell it as an argument; the two that take no input at all
    # (`issueArchive`, `cycleDelete`) carry it as a field argument, because
    # inventing a one-field input object for them would be worse.
    #
    # A slug and not a workspace id, deliberately. CLAUDE.md forbids trusting
    # a workspace id from the frontend: the slug is a public string that
    # selects WHAT is being asked about, and `app.graphql.scope` decides
    # whether the session behind the request may act there.
    workspace_slug: str

    issue_id: UUID
    body: str


@strawberry.input
class CommentDeleteInput:
    workspace_slug: str
    id: UUID
