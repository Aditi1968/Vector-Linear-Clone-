"""Transport types for an issue's history.

Separate from `app.graphql.types.notification` because the two are separate
concepts and not two views of one: this is what happened to an issue and reads
the same for everybody; that is what one person still has to look at.
"""

import enum
from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.activity import ActivityEntity, ActivityPage
from app.graphql.types.pagination import PageInfo


# Page size for `Issue.activity` when a document does not say.
#
# Ten, matching `DEFAULT_RELATED_FIRST` and for its arithmetic rather than for
# symmetry. `app.graphql.limits` prices a nested list field at
# (outer page size) x (inner page size) x (fields below it) and charges the
# SCHEMA's default when a document omits the argument, so at fifty a page of
# issues each asking for its history would be refused outright. Ten keeps the
# ordinary nested query askable; a single issue's timeline is charged once and
# is cheap at any page size the service allows.
DEFAULT_ACTIVITY_FIRST = 10


@strawberry.enum(name="IssueActivityKind")
class ActivityKindEnum(enum.Enum):
    """Every event an issue's history records.

    Declared here rather than by decorating `app.domain.activity.ActivityKind`
    with `strawberry.enum`, which would work and would put a Strawberry
    attribute on a domain class -- the one thing the domain layer must not
    carry. The values are identical to the domain enum's, and
    tests/test_activity_notifications.py fails if the two ever drift.
    """

    CREATED = "created"
    TITLE_CHANGED = "title_changed"
    STATE_CHANGED = "state_changed"
    PRIORITY_CHANGED = "priority_changed"
    ASSIGNEE_CHANGED = "assignee_changed"
    ARCHIVED = "archived"
    COMMENTED = "commented"
    LABEL_ATTACHED = "label_attached"
    LABEL_DETACHED = "label_detached"
    RELATION_ADDED = "relation_added"
    PROJECT_CHANGED = "project_changed"
    CYCLE_CHANGED = "cycle_changed"


@strawberry.type(name="IssueActivity")
class ActivityType:
    """One thing that happened to an issue.

    `actor_id` and not `actor`, matching `Comment.authorId`: resolving it to a
    `User` means a type this feature does not own and a second batched read
    per page, and the id is what a client needs to key an avatar it already
    has. Null means nobody to name -- a system action, or an actor whose
    account has since been deleted, which a reader cannot and need not tell
    apart.

    `from_value` and `to_value` are strings whose meaning depends on `kind`,
    and rendering them is the client's job because it is the layer that knows
    how to turn a workflow-state id into "In Progress". They are deliberately
    not pre-rendered on the server: a sentence built here would be a sentence
    in one language, frozen at write time.
    """

    id: UUID
    issue_id: UUID
    actor_id: UUID | None
    kind: ActivityKindEnum
    from_value: str | None
    to_value: str | None
    caused_by: str | None = strawberry.field(
        description=(
            "What caused this, when `actorId` is null and it was not nobody. "
            "Null for everything a person did -- the actor is the cause. "
            "Opaque text with a documented prefix; today the only one is "
            "`github_pull_request:owner/name#84`, written when a pull request "
            "moved the issue. A client that does not recognise a prefix should "
            "render the row exactly as it renders one with no cause, which is "
            "what keeps a second cause from being a breaking change."
        )
    )
    created_at: datetime

    @classmethod
    def from_entity(cls, entity: ActivityEntity) -> "ActivityType":
        return cls(
            id=entity.id,
            issue_id=entity.issue_id,
            actor_id=entity.actor_id,
            kind=ActivityKindEnum(entity.kind.value),
            from_value=entity.from_value,
            to_value=entity.to_value,
            caused_by=entity.caused_by,
            created_at=entity.created_at,
        )


@strawberry.type
class IssueActivityConnection:
    nodes: list[ActivityType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: ActivityPage) -> "IssueActivityConnection":
        return cls(
            nodes=[ActivityType.from_entity(entity) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )
