from uuid import UUID

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


@strawberry.input
class GithubRepositoriesSetInput:
    """Which of the installation's repositories this workspace tracks.

    The WHOLE set, every time, because that is what a page of checkboxes
    submits: sending only the changes would let two admins saving two
    selections interleave into a set neither of them chose.

    `repositoryIds` are GitHub's own ids, as `GithubRepository.repositoryId`
    reports them, and they are IDs rather than Ints for the reason that type
    gives -- GraphQL's Int is signed 32-bit and GitHub's counter is not.
    An id naming a repository this installation does not cover matches nothing
    and is ignored; this narrows what GitHub granted and can never widen it.

    An EMPTY list is meaningful and is not the same as omitting the field:
    it tracks nothing, which is a workspace that has connected GitHub and
    wants no development activity yet.
    """

    workspace_slug: str
    repository_ids: list[strawberry.ID]


@strawberry.input
class GithubAutomationSetInput:
    """What a pull request does to one team's issues.

    `enabled: false` turns the automation off and removes the configuration,
    which is what off is -- there is no row for a disabled automation, so
    there is nothing for the two state fields to mean and they are ignored.

    `enabled: true` with NEITHER state named asks for the default derived from
    category: the team's first started state and first completed state, in
    board order. What gets stored is those two ids explicitly, so the settings
    screen shows two named states rather than a rule that will pick one later,
    and a team that adds a second started state tomorrow does not silently
    change what its automation does.

    Naming a state explicitly is checked against this team's own board and
    against the slot's category: a started slot must name a `started` state.
    Both failures answer the same field error, so an id belonging to another
    team is reported exactly as a typo -- which is what stops this confirming
    that a guessed id names a real state somewhere.

    Naming ONE state and leaving the other null is a real configuration: the
    named half fires and the other does nothing.
    """

    workspace_slug: str
    team_id: UUID
    enabled: bool
    started_state_id: UUID | None = None
    completed_state_id: UUID | None = None
