from uuid import UUID

import strawberry


@strawberry.input
class LabelCreateInput:
    """A new label.

    `color` is nullable and defaults to null, which means "no preference"
    rather than "no colour": `LabelService.DEFAULT_COLOR` is substituted
    there. The default is not written here because a default declared in the
    transport is a rule only GraphQL callers get, and the service is the layer
    a worker or a REST endpoint would go through too.
    """

    name: str
    color: str | None = None


@strawberry.input
class LabelUpdateInput:
    """A replacement for a label's editable fields.

    Both fields are required, so this is a replacement and not a patch. A
    partial update would need "leave this alone" to be distinguishable from
    "set this to null", and neither field is nullable -- so the distinction
    would exist only to be explained. A client that rendered the label it is
    editing already holds both values.
    """

    id: UUID
    name: str
    color: str


@strawberry.input
class LabelDeleteInput:
    id: UUID


@strawberry.input
class IssueLabelInput:
    """Which label, on which issue.

    No workspace. The pair is resolved inside the workspace the request is
    already bound to, so a client cannot name a tenant here -- which is the
    point: an issue id from one workspace and a label id from another do not
    describe a cross-tenant attachment, they describe two ids at least one of
    which does not exist in the workspace this request is in.
    """

    issue_id: UUID
    label_id: UUID
