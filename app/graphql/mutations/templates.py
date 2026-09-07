import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.template import (
    IssueCreateFromTemplateInput,
    IssueTemplateCreateInput,
    IssueTemplateDeleteInput,
    IssueTemplateUpdateInput,
)
from app.graphql.scope import authorized_scope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueType
from app.graphql.types.template import (
    IssueCreateFromTemplatePayload,
    IssueTemplateDeletePayload,
    IssueTemplateSavePayload,
    IssueTemplateType,
)


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class TemplateMutation:
    """Templates, and filing an issue from one, merged into the root Mutation.

    Every resolver here begins by resolving the workspace from a slug against
    the caller's memberships, and passes THAT scope down. Nothing takes a
    workspace id from the document, and nothing reads a workspace out of a
    stored template -- which is the property that makes applying a template
    safe: the tenant comes from who is asking, never from what was saved.

    There are no role checks. Any member may write a template, which is the
    same rule labels and cycles have and is deliberately looser than
    invitations: a template is a convenience anybody can create and anybody can
    ignore. When the product grows notification-shaped consequences for
    templates -- an auto-assign that fires without a person -- that is the
    moment to revisit it, and `require_workspace_admin` is where it would go.
    """

    @strawberry.mutation
    async def issue_template_create(
        self,
        info: Info,
        input: IssueTemplateCreateInput,
    ) -> IssueTemplateSavePayload:
        """Save one new template.

        Every id it names -- team, assignee, project, cycle, labels -- is
        checked against THIS workspace by the composite foreign keys migration
        020 declares, not by a lookup here. A refusal comes back as a field
        error naming the half of the form that was wrong, and never
        distinguishes "belongs to another workspace" from "does not exist".
        """
        # Resolved before the try, and outside it. An unauthenticated caller
        # and one who may not see this workspace are not things the client's
        # INPUT can be corrected to fix.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.template_service.create(
                scope=scope,
                draft=input.template.to_draft(),
            )
        except ValidationError as exc:
            # Only expected input validation reaches the payload. Everything
            # else propagates through GraphQL's error mechanism and is masked.
            return IssueTemplateSavePayload(template=None, errors=_errors(exc))

        return IssueTemplateSavePayload(
            template=IssueTemplateType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def issue_template_update(
        self,
        info: Info,
        input: IssueTemplateUpdateInput,
    ) -> IssueTemplateSavePayload:
        """Replace one template wholesale.

        A replace and not a patch: what the input carries is the template
        afterwards, so a default left null is a default cleared.

        A template in another workspace and one that does not exist answer the
        same "Template not found".
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.template_service.update(
                scope=scope,
                template_id=input.id,
                draft=input.template.to_draft(),
            )
        except ValidationError as exc:
            return IssueTemplateSavePayload(template=None, errors=_errors(exc))

        return IssueTemplateSavePayload(
            template=IssueTemplateType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def issue_template_delete(
        self,
        info: Info,
        input: IssueTemplateDeleteInput,
    ) -> IssueTemplateDeletePayload:
        """Discard one template.

        The issues already filed from it are untouched, and there is no link
        between them to break: a template is a starting shape, and an issue
        that was once filed from one is an ordinary issue afterwards.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            deleted = await info.context.template_service.delete(
                scope=scope,
                template_id=input.id,
            )
        except ValidationError as exc:
            return IssueTemplateDeletePayload(id=None, errors=_errors(exc))

        return IssueTemplateDeletePayload(id=deleted, errors=[])

    @strawberry.mutation
    async def issue_create_from_template(
        self,
        info: Info,
        input: IssueCreateFromTemplateInput,
    ) -> IssueCreateFromTemplatePayload:
        """File one issue from a template.

        The template is read under the scope this call authorized, so a
        template id belonging to another tenant resolves to nothing and answers
        "Template not found" -- the same answer an id that exists nowhere gets.
        Nothing downstream ever holds another workspace's defaults.

        Every id the template carries is then re-checked, by being handed to
        the same composite foreign keys `issueCreate` goes through. That is not
        redundant with the check at save time: it is what makes this path
        correct without depending on the save path having been correct, which
        is the property that survives a bulk import or a hand-written row.

        Authorship comes from the membership row that authorized this call, and
        there is no input field for it -- a client able to name a creator could
        forge one.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.template_service.apply(
                scope=scope,
                template_id=input.template_id,
                team_id=input.team_id,
                title=input.title,
                actor_id=scope.user_id,
            )
        except ValidationError as exc:
            return IssueCreateFromTemplatePayload(issue=None, errors=_errors(exc))

        return IssueCreateFromTemplatePayload(
            issue=IssueType.from_entity(entity, scope),
            errors=[],
        )
