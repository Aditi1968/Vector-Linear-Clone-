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

    issue_id: UUID
    body: str


@strawberry.input
class CommentDeleteInput:
    id: UUID
