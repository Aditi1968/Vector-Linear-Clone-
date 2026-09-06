import strawberry


@strawberry.input
class GithubDisconnectInput:
    """Which workspace to disconnect.

    A slug and nothing else. There is deliberately no installation id: the
    caller does not get to name what is removed, only which workspace's
    integration is theirs to remove -- and which workspace that is gets
    checked against `workspace_members` before anything is deleted.
    """

    workspace_slug: str
