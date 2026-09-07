from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.labels import LabelEntity, LabelGroupEntity
from app.domain.pagination import LabelPage
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo


@strawberry.type(name="Label")
class LabelType:
    id: UUID
    name: str
    color: str

    group_id: UUID | None = strawberry.field(
        description=(
            "The label group this label belongs to, or null for one that "
            "belongs to none. An id rather than the group itself: a label "
            "picker renders a hundred of these and would otherwise resolve a "
            "group per row, and the groups are already on the page through "
            "`labelGroups`."
        )
    )

    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: LabelEntity) -> "LabelType":
        return cls(
            id=entity.id,
            name=entity.name,
            color=entity.color,
            group_id=entity.group_id,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type(name="LabelGroup")
class LabelGroupType:
    """A parent for labels, and optionally a rule about wearing them.

    There is deliberately no `labels` field here. `Label.groupId` already
    carries the edge from the other end, and every screen that renders groups
    renders the labels too -- so one `labels(workspaceSlug:)` page plus one
    `labelGroups(workspaceSlug:)` list is the whole picker, grouped by the
    client from data it already holds. A field here would be a second query per
    group for an edge the first query already returned.
    """

    id: UUID
    name: str

    exclusive: bool = strawberry.field(
        description=(
            "Whether an issue may wear at most one label from this group. "
            "Enforced by the database, not by this server: attaching a second "
            "label from an exclusive group is refused, and so is turning "
            "exclusivity on for a group whose labels already share an issue."
        )
    )

    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: LabelGroupEntity) -> "LabelGroupType":
        return cls(
            id=entity.id,
            name=entity.name,
            exclusive=entity.exclusive,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class LabelGroupPayload:
    """The answer to labelGroupCreate and labelGroupUpdate alike.

    One payload for both, for the reason `LabelPayload` gives: both answer the
    same question, and a separate type with identical fields would be two names
    for one shape that every client has to handle.
    """

    group: LabelGroupType | None
    errors: list[ValidationErrorType]


@strawberry.type
class LabelGroupDeletePayload:
    """What was removed, so a client can evict it without guessing.

    The labels that were in the group are NOT returned. They still exist and
    still sit on every issue they were on -- only the grouping went -- so a
    client's own label list is stale in one field and refetching it is the
    honest fix; listing them here would make the payload look like a cascade it
    is not.
    """

    deleted_group_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class LabelPayload:
    """The answer to labelCreate and labelUpdate alike.

    One payload for both because both answer the same question -- here is the
    label, or here is what was wrong with the request -- and a client that can
    read one can read the other. A separate LabelUpdatePayload with identical
    fields would be two names for one shape, and every client would have to
    handle both.
    """

    label: LabelType | None
    errors: list[ValidationErrorType]


@strawberry.type
class LabelDeletePayload:
    """What was removed, so a client can evict it without guessing.

    The id is echoed rather than left implicit. A cache that removed whatever
    id it had just sent would be trusting its own request; this reports what
    the server actually deleted, and reports null when it deleted nothing.
    """

    deleted_label_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class LabelConnection:
    nodes: list[LabelType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: LabelPage) -> "LabelConnection":
        return cls(
            nodes=[LabelType.from_entity(entity) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )
