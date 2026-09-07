from uuid import UUID

import strawberry


@strawberry.input
class IssueSubscriptionInput:
    """Which issue the viewer is starting or stopping watching.

    One input for both mutations, because both take exactly these two values
    and a second class differing only in its name would be one more thing to
    keep in step. The verb is the field name in the schema, which is where a
    reader looks for it.

    `workspaceSlug` and not a workspace id, matching every other slug-taking
    field: a slug is a public string a client may spell, and the server
    resolves it against the caller's memberships. An id accepted from a client
    would be a tenant the client chose.

    There is no `userId`. The watcher is the authenticated viewer and nothing
    else could be right, so there is no field through which a caller could sign
    somebody else up for an issue's notifications.
    """

    workspace_slug: str
    issue_id: UUID
