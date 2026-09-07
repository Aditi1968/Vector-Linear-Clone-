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
class LabelGroupCreateInput:
    """A new label group.

    `exclusive` is nullable and defaults to null, which means "no preference"
    rather than "not exclusive": `LabelService.DEFAULT_GROUP_EXCLUSIVE` is
    substituted there. The default is not written here because a default
    declared in the transport is a rule only GraphQL callers get, and the
    service is the layer a worker or a REST endpoint would go through too --
    the same argument `LabelCreateInput` makes about `color`.
    """

    workspace_slug: str

    name: str
    exclusive: bool | None = None


@strawberry.input
class LabelGroupUpdateInput:
    """A replacement for a label group's editable fields.

    Both fields are required, so this is a replacement and not a patch, for the
    reason `LabelUpdateInput` gives: a client that rendered the group it is
    editing already holds both values, and neither field is nullable, so
    "leave this alone" would have no spelling distinct from "set this to null".

    Sending `exclusive: true` for a group whose labels already share an issue
    is refused, and refused for the whole group rather than for the offending
    issues. See `LabelService.update_group`.
    """

    workspace_slug: str
    id: UUID
    name: str
    exclusive: bool


@strawberry.input
class LabelGroupDeleteInput:
    workspace_slug: str
    id: UUID


@strawberry.input
class LabelSetGroupInput:
    """Which label moves into which group, in which workspace.

    `groupId` is nullable and null MEANS something here, unlike on the create
    input above: it is the request to take the label out of whatever group it
    is in. There is no separate ungroup mutation because there is no separate
    state -- a label's group is one nullable value, and two mutations writing
    it would be two paths into one column.
    """

    workspace_slug: str
    label_id: UUID
    group_id: UUID | None = None


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
