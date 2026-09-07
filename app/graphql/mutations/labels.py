import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.domain.tenancy import WorkspaceScope
from app.graphql.inputs.label import (
    IssueLabelInput,
    LabelCreateInput,
    LabelDeleteInput,
    LabelGroupCreateInput,
    LabelGroupDeleteInput,
    LabelGroupUpdateInput,
    LabelSetGroupInput,
    LabelUpdateInput,
)
from app.graphql.scope import authorized_scope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueLabelPayload, IssueType
from app.graphql.types.label import (
    LabelDeletePayload,
    LabelGroupDeletePayload,
    LabelGroupPayload,
    LabelGroupType,
    LabelPayload,
    LabelType,
)


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    """The domain's structured issues, as the transport reports them."""
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


async def _issue_payload(
    info: Info,
    scope: WorkspaceScope,
    input: IssueLabelInput,
) -> IssueLabelPayload:
    """Re-read the issue the change landed on.

    Read back rather than reconstructed, so the payload reports the row as it
    now is. `issue` is nullable for the error case at the call sites and stays
    nullable here for a reason that is not merely typing: the issue can be
    deleted between the write and this read, and answering null is honest
    where inventing a stale object would not be.

    A module-level function, NOT a method, and that is the whole point rather
    than a preference. The root types are assembled by `strawberry.tools.
    merge_types`, so a root resolver's `self` is the root value -- which is
    None. These two resolvers were the only ones in the schema that reached
    through `self`, and `self._issue_payload` therefore raised AttributeError
    AFTER the service call had already committed: the label really was
    attached, the activity row really was written, and the client got
    `data: null` with a masked "Internal server error", so a retry then
    reported the label as already attached. Nothing here may depend on `self`.
    """
    entity = await info.context.issue_service.get_by_id(
        scope=scope,
        issue_id=input.issue_id,
    )

    return IssueLabelPayload(
        issue=None if entity is None else IssueType.from_entity(entity, scope),
        errors=[],
    )


@strawberry.type
class LabelMutation:
    """The write half of labels, merged into the root Mutation.

    Every resolver here is the same three lines: resolve the workspace, call
    the service, and turn the one expected exception into a payload. Nothing
    decides anything -- an id that names nothing, a duplicate name, a label
    already applied are all rules the service and the schema own between them,
    and a resolver that re-checked any of them would be a second copy that can
    disagree with the first.

    Every one of them requires an authenticated viewer who is a member of the
    workspace named in the input, because `authorized_scope` resolves the
    viewer first and the membership second. There is no anonymous path into
    any of these any more.
    """

    @strawberry.mutation
    async def label_create(self, info: Info, input: LabelCreateInput) -> LabelPayload:
        # Resolved before the try, and outside it. An unauthenticated caller,
        # and one who may not see this workspace, are not things the client's
        # INPUT can be corrected to fix, so they leave as GraphQL errors
        # rather than as field errors on the payload.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.label_service.create(
                scope=scope,
                name=input.name,
                color=input.color,
            )
        except ValidationError as exc:
            # Only expected input validation is translated into the payload.
            # Everything else (asyncpg failures, bugs, outages) propagates
            # through GraphQL's normal error mechanism and is masked.
            return LabelPayload(label=None, errors=_errors(exc))

        return LabelPayload(label=LabelType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def label_update(self, info: Info, input: LabelUpdateInput) -> LabelPayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.label_service.update(
                scope=scope,
                label_id=input.id,
                name=input.name,
                color=input.color,
            )
        except ValidationError as exc:
            return LabelPayload(label=None, errors=_errors(exc))

        return LabelPayload(label=LabelType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def label_delete(
        self, info: Info, input: LabelDeleteInput
    ) -> LabelDeletePayload:
        """Delete a label, and with it every issue's use of it.

        `issue_labels_label_fk ON DELETE CASCADE` removes the associations. No
        issue is touched: an association carries no data of its own, and
        deleting a label is exactly how a workspace stops using one.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            deleted_id = await info.context.label_service.delete(
                scope=scope,
                label_id=input.id,
            )
        except ValidationError as exc:
            return LabelDeletePayload(deleted_label_id=None, errors=_errors(exc))

        return LabelDeletePayload(deleted_label_id=deleted_id, errors=[])

    @strawberry.mutation
    async def label_group_create(
        self, info: Info, input: LabelGroupCreateInput
    ) -> LabelGroupPayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.label_service.create_group(
                scope=scope,
                name=input.name,
                exclusive=input.exclusive,
            )
        except ValidationError as exc:
            return LabelGroupPayload(group=None, errors=_errors(exc))

        return LabelGroupPayload(group=LabelGroupType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def label_group_update(
        self, info: Info, input: LabelGroupUpdateInput
    ) -> LabelGroupPayload:
        """Rename a group, or change whether it is exclusive.

        Turning exclusivity ON can be refused with GROUP_IN_USE, and the
        refusal is the database's rather than this server's: migration 021
        propagates the flag down to `issue_labels`, so an issue already wearing
        two of this group's labels makes the update violate a unique index. The
        resolver does not decide that and could not check it without a race.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.label_service.update_group(
                scope=scope,
                group_id=input.id,
                name=input.name,
                exclusive=input.exclusive,
            )
        except ValidationError as exc:
            return LabelGroupPayload(group=None, errors=_errors(exc))

        return LabelGroupPayload(group=LabelGroupType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def label_group_delete(
        self, info: Info, input: LabelGroupDeleteInput
    ) -> LabelGroupDeletePayload:
        """Delete a group; its labels survive, ungrouped.

        Nothing cascades onto an issue. The labels keep every issue they were
        on and only the grouping goes -- which also means every exclusivity
        rule the group imposed stops applying, without any issue changing.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            deleted_id = await info.context.label_service.delete_group(
                scope=scope,
                group_id=input.id,
            )
        except ValidationError as exc:
            return LabelGroupDeletePayload(deleted_group_id=None, errors=_errors(exc))

        return LabelGroupDeletePayload(deleted_group_id=deleted_id, errors=[])

    @strawberry.mutation
    async def label_set_group(
        self, info: Info, input: LabelSetGroupInput
    ) -> LabelPayload:
        """Move a label into a group, or out of the one it is in.

        The payload carries the LABEL rather than the group, because the label
        is what changed: a group's own row is untouched by this.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.label_service.set_label_group(
                scope=scope,
                label_id=input.label_id,
                group_id=input.group_id,
            )
        except ValidationError as exc:
            return LabelPayload(label=None, errors=_errors(exc))

        return LabelPayload(label=LabelType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def issue_label_attach(
        self, info: Info, input: IssueLabelInput
    ) -> IssueLabelPayload:
        """Apply a label to an issue, both in this request's workspace.

        The payload carries the ISSUE rather than the association, so a client
        can re-select `issue { labels { ... } }` in the same round trip and see
        the result instead of inferring it. That read costs one statement after
        the write; the alternative is a client that updates its cache from what
        it hoped the server did.
        """
        scope = await authorized_scope(info, input.workspace_slug)
        actor_id = scope.user_id

        try:
            await info.context.label_service.attach(
                scope=scope,
                issue_id=input.issue_id,
                label_id=input.label_id,
                actor_id=actor_id,
            )
        except ValidationError as exc:
            return IssueLabelPayload(issue=None, errors=_errors(exc))

        return await _issue_payload(info, scope, input)

    @strawberry.mutation
    async def issue_label_detach(
        self, info: Info, input: IssueLabelInput
    ) -> IssueLabelPayload:
        scope = await authorized_scope(info, input.workspace_slug)
        actor_id = scope.user_id

        try:
            await info.context.label_service.detach(
                scope=scope,
                issue_id=input.issue_id,
                label_id=input.label_id,
                actor_id=actor_id,
            )
        except ValidationError as exc:
            return IssueLabelPayload(issue=None, errors=_errors(exc))

        return await _issue_payload(info, scope, input)
