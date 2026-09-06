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

    # Every workspace-scoped mutation names its tenant, and every mutation
    # that takes an `input` names it HERE rather than beside the input. One
    # place per operation, so a client never has to remember which mutations
    # spell it as an argument; the two that take no input at all
    # (`issueArchive`, `cycleDelete`) carry it as a field argument, because
    # inventing a one-field input object for them would be worse.
    #
    # A slug and not a workspace id, deliberately. CLAUDE.md forbids trusting
    # a workspace id from the frontend: the slug is a public string that
    # selects WHAT is being asked about, and `app.graphql.scope` decides
    # whether the session behind the request may act there.
    workspace_slug: str

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

    workspace_slug: str
    id: UUID
    name: str
    color: str


@strawberry.input
class LabelDeleteInput:
    workspace_slug: str
    id: UUID


@strawberry.input
class IssueLabelInput:
    """Which label, on which issue, in which workspace.

    One workspace for the pair, and there can never be two. An issue id from
    one workspace and a label id from another do not describe a cross-tenant
    attachment; they describe two ids at least one of which does not exist in
    the workspace the caller was authorized for, and the composite foreign
    keys on `issue_labels` are what make that true of the database rather than
    only of this file.
    """

    workspace_slug: str
    issue_id: UUID
    label_id: UUID
