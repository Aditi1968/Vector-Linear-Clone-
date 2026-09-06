from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.labels import LabelEntity
from app.domain.pagination import LabelPage
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo


@strawberry.type(name="Label")
class LabelType:
    id: UUID
    name: str
    color: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: LabelEntity) -> "LabelType":
        return cls(
            id=entity.id,
            name=entity.name,
            color=entity.color,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


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
