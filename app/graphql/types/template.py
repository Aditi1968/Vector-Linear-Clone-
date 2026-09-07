"""Transport types for the shapes a new issue can be filed from."""

from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.templates import IssueTemplateEntity
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueType


@strawberry.type(name="IssueTemplate")
class IssueTemplateType:
    """One stored template.

    `name` is what the template is called in the menu; `title` is the title an
    issue filed from it starts with. They are different strings and the
    distinction is easy to lose -- a template called "Bug report" whose issues
    are all titled "Bug report" is what happens when one field tries to be
    both.

    Every default is nullable, and null means "this template has no opinion"
    rather than "empty". A client renders a null default by leaving the field
    for the author.

    Ids and not resolved objects, for the reason `IssueSubscriberType` gives:
    a template is read on the way into a create form, and a form that resolved
    a project, a cycle, a member and five labels would pay for nine queries to
    render one menu entry. Every id here is one the database has confirmed
    belongs to this workspace -- but a client should still render from lists it
    already holds rather than treat these as proof of anything.
    """

    id: UUID
    team_id: UUID | None
    name: str
    title: str | None
    description: str | None
    priority: int | None
    estimate: int | None
    assignee_id: UUID | None
    project_id: UUID | None
    cycle_id: UUID | None
    label_ids: list[UUID]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: IssueTemplateEntity) -> "IssueTemplateType":
        return cls(
            id=entity.id,
            team_id=entity.team_id,
            name=entity.name,
            title=entity.title,
            description=entity.description,
            priority=entity.priority,
            estimate=entity.estimate,
            assignee_id=entity.assignee_id,
            project_id=entity.project_id,
            cycle_id=entity.cycle_id,
            # A list on the wire, a tuple in the domain. GraphQL has one
            # sequence kind and the entity is frozen; the conversion is the
            # boundary doing its job rather than a leak either way.
            label_ids=list(entity.label_ids),
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class IssueTemplateSavePayload:
    """The template as it now is, for a create or an update.

    One payload for both, because a save REPLACES the record -- so what comes
    back is the same thing in both cases, and two types differing only in their
    name would be one more pair to keep in step.

    Null beside a non-empty `errors` covers every refusal: a field the client
    can correct, and an id naming a template this workspace does not have.
    """

    template: IssueTemplateType | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueTemplateDeletePayload:
    """The id that went, so a client can drop it from a list it already holds.

    The id rather than the row: a client deleting a template is about to stop
    showing it, and returning the record it just discarded invites rendering
    something that no longer exists.
    """

    id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueCreateFromTemplatePayload:
    """The issue the template produced.

    A full `Issue`, not a summary: the client is about to navigate to it, and
    every field it needs is on the row the create already returned.

    ponytail: applying a template is four writes across two services and is not
    one transaction -- see `TemplateService.apply`. A failure part way through
    surfaces as an error here with the issue already filed, so a client that
    retries would file a second issue. Making it atomic needs a
    connection-taking `IssueService.create`, which is its own commit.
    """

    issue: IssueType | None
    errors: list[ValidationErrorType]
