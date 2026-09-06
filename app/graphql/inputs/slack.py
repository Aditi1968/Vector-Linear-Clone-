import strawberry


@strawberry.input
class SlackDisconnectInput:
    """Which workspace's Slack integration to remove.

    A slug and nothing else. In particular there is no installation id and no
    Slack team id: a workspace has one installation, so naming it a second way
    would be an argument a client could get wrong -- and an id argument is the
    shape that lets a caller name a row belonging to someone else's tenant and
    find out whether it exists.

    An input type for one field, matching every other mutation here. The
    argument is `input:` so that a second field can be added later without
    changing the mutation's signature, which is the whole convention.
    """

    workspace_slug: str
