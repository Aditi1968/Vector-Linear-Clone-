from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.comments import CommentEntity
from app.domain.pagination import CommentPage
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo


# The page size `Issue.comments` declares.
#
# Smaller than the 50 `issues` uses, and the difference is the point: this
# field is NESTED, so its cost is multiplied by the page of issues above it.
# app/graphql/limits.py charges a list field `page size * inner cost`, and it
# reads the declared default when a document omits the argument -- so this
# number is what an unqualified `comments` costs inside every issue on a page.
#
# The arithmetic a client should know: on a page of issues, selecting two
# scalars per comment costs 20 * 2 = 40 per issue, which puts
# `issues { nodes { comments { nodes { id body } } } }` at 50 * 40 = 2000 and
# over MAX_COMPLEXITY (1000). Asking for `issues(first: 20)` brings it back
# under. A comment thread read on ONE issue -- `issue(id:) { comments { ... } }`
# -- is charged once and is cheap at any page size the service allows.
DEFAULT_COMMENT_FIRST = 20


@strawberry.type(name="Comment")
class CommentType:
    """One comment, as the wire sees it.

    `author_id` and not `author`. Resolving the author to a `User` object
    means a type this phase does not own -- authentication owns `users` -- and
    a second batched read per page. The id is what a client needs to key an
    avatar it already has, and adding the object later is an additive schema
    change; removing a wrongly-shaped one is not.

    `edited_at` is null for a comment nobody has edited, which is a different
    claim from `updated_at`. No mutation sets it yet: commentUpdate is not in
    this phase, so today it is always null and the field is here so that it
    does not have to be back-filled with an edit history that never existed.
    """

    id: UUID
    issue_id: UUID
    author_id: UUID
    body: str
    edited_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: CommentEntity) -> "CommentType":
        return cls(
            id=entity.id,
            issue_id=entity.issue_id,
            author_id=entity.author_id,
            body=entity.body,
            edited_at=entity.edited_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class CommentCreatePayload:
    comment: CommentType | None
    errors: list[ValidationErrorType]


@strawberry.type
class CommentDeletePayload:
    """What was removed, so a client can evict it without guessing.

    Null when nothing was deleted -- which is also the answer for a comment
    that belongs to another workspace, because those two must not be
    distinguishable.
    """

    deleted_comment_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class CommentConnection:
    nodes: list[CommentType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: CommentPage) -> "CommentConnection":
        return cls(
            nodes=[CommentType.from_entity(entity) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )
