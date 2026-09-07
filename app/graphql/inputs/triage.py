from uuid import UUID

import strawberry


@strawberry.input
class TriageEnterInput:
    """Which issue moves into its own team's queue.

    There is deliberately no `teamId`. A queue is per team and an issue belongs
    to exactly one, so the queue an issue enters is the queue of the team it is
    already on; a client able to name a different one would be filing work into
    a queue the issue is not in. Moving it is `triageChangeTeam`, which
    renumbers the issue rather than pretending it did not move.
    """

    # Every workspace-scoped mutation names its tenant, and a slug rather than
    # a workspace id: CLAUDE.md forbids trusting a workspace id from the
    # frontend, so the slug selects WHAT is being asked about and
    # `app.graphql.scope` decides whether the session behind the request may
    # act there.
    workspace_slug: str
    issue_id: UUID


@strawberry.input
class TriageAcceptInput:
    """Which issue leaves the queue, and into which state.

    `workflowStateId` is required, because accepting IS choosing a state. An
    issue in triage carries one only because the column is NOT NULL and nobody
    picked the value it holds, so a default here would be this input deciding
    what a team's incoming work becomes.

    The state must belong to the issue's own team. That is refused by
    `issues_workflow_state_fk` rather than checked here, against the team the
    database has stored rather than one a client asserted.
    """

    workspace_slug: str
    issue_id: UUID
    workflow_state_id: UUID


@strawberry.input
class TriageDeclineInput:
    """Which issue is refused.

    No state to choose. Declining is the decision that this work will not be
    done, and the state it lands in -- the team's canceled state -- is a
    consequence of that decision rather than a second choice. A caller able to
    name it could decline an issue into `In Progress`, which the board would
    then report as work in flight.
    """

    workspace_slug: str
    issue_id: UUID


@strawberry.input
class TriageMarkDuplicateInput:
    """Which issue is a duplicate, and of what.

    Records an ordinary `duplicate` row in `issue_relations` and then declines
    the issue. There is no second duplicate mechanism -- no `duplicateOf` field
    on Issue, no triage-local table -- because migration 010 already owns that
    vocabulary and canonicalises the pair, so the relation reads correctly from
    both ends and cannot be stored twice.
    """

    workspace_slug: str
    issue_id: UUID
    duplicate_of_id: UUID


@strawberry.input
class TriageChangeTeamInput:
    """Which queued issue moves to which team.

    The issue is RENUMBERED -- ENG-9 becomes DES-4 -- which nothing else in
    this product does, because an identifier is the issue's name outside the
    database. It is defensible here and only here: an issue still in triage has
    not been accepted by anyone, nothing links to it, and the team it was filed
    against was a guess. The server refuses this for any issue that has left a
    queue.

    The issue stays in triage, on the new team's queue. Routing work to the
    right team is not deciding what to do with it.
    """

    workspace_slug: str
    issue_id: UUID
    team_id: UUID
