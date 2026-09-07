"""Triage: a team's queue of work nobody has accepted yet.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.

Distinct from the inbox, and the two are not layers of one thing. A
notification (`app.domain.notifications`) is something ONE PERSON has to look
at, addressed to them and read by them alone. A triage entry is something A
TEAM has to decide about: it is addressed to nobody, everyone on the team sees
the same queue, and it stops existing when a decision is made rather than when
somebody has read it.

An issue in triage has not been given a status anyone chose. It still holds a
`workflow_state_id`, because migration 005 made that column NOT NULL and an
issue with no status is not a row this schema can store -- but that value is
whatever the issue was filed with, and no board should be read as showing it.
Leaving triage is what puts the issue in a state somebody picked.

There are three ways out -- accepted into a chosen state, declined onto the
team's canceled state, and declined with a `duplicate` row in `issue_relations`
saying which issue it repeats -- and none of them is recorded on `issues`. The
outcome is already stored twice over, in the state the issue ended up in and in
the activity row that says who moved it there, so a third copy would be a
column that can disagree with both. In particular there is no `duplicate_of`
column and no triage-local duplicate table: migration 010 owns that vocabulary,
canonicalises the pair, and refuses the second insert.
"""

from dataclasses import dataclass
from datetime import datetime

from app.domain.issues import IssueEntity


@dataclass(frozen=True, slots=True)
class TriageIssueEntity:
    """One issue waiting in a team's queue, and how long it has waited.

    Composition rather than a wider `IssueEntity`. `triage_entered_at` is
    meaningful for exactly the rows in this queue and is NULL for every other
    issue in the database, so putting it on the entity would add a column to
    every issue read in the product -- and to `IssueRepository.ISSUE_COLUMNS`,
    to the fake row builder in the test suite, and to every construction site
    of an entity that has nothing to do with triage.

    The trade is that a caller wanting the issue reaches through `.issue`. That
    is one attribute, and it is honest about which of the two facts is the
    issue and which is the queue's.
    """

    issue: IssueEntity

    # When this issue entered the queue. Non-NULL by construction: a row with
    # NULL here is not in any queue and this type does not describe it.
    #
    # Also the ordering key. The queue is read oldest first -- the thing that
    # has waited longest is the thing somebody has to look at -- and the
    # keyset cursor is `(entered_at, id)`, which `issues_workspace_team_triage_idx`
    # serves without a sort.
    entered_at: datetime


@dataclass(frozen=True, slots=True)
class TriageIssuePage:
    """A keyset page of one team's queue.

    Separate from `IssuePage` rather than generic over its node type, for the
    reason `IssueRelationPage` gives: the two carry different entities and feed
    different GraphQL connections, and a shared generic would buy one saved
    dataclass at the cost of a type parameter in every signature that mentions
    either.
    """

    nodes: list[TriageIssueEntity]
    has_next_page: bool
    end_cursor: str | None
