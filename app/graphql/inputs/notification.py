from uuid import UUID

import strawberry


@strawberry.input
class NotificationMarkReadInput:
    """Mark one of the viewer's own notifications read.

    `workspaceSlug` and not a workspace id, matching every other slug-taking
    field: a slug is a public string a client may spell, and the server
    resolves it against the caller's memberships. An id accepted from a
    client would be a tenant the client chose.

    There is no `userId`. The recipient is the authenticated viewer and
    nothing else could be right, so there is no field through which a caller
    could name somebody else's inbox.
    """

    workspace_slug: str
    id: UUID


@strawberry.input
class NotificationMarkAllReadInput:
    """Mark every unread notification the viewer has in one workspace.

    Scoped to one workspace rather than to all of them, deliberately: a
    caller's inboxes are per workspace, and "clear everything everywhere" is
    a different request that nothing has asked for. It would also be one
    mutation whose blast radius a client could not see before sending it.
    """

    workspace_slug: str
