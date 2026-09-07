from uuid import UUID

import strawberry

from app.domain.templates import IssueTemplateDraft


@strawberry.input
class IssueTemplateFieldsInput:
    """Everything one save of a template says, minus which template it is.

    Nested inside the create and update inputs rather than repeated in both,
    and the nesting is not only to avoid ten duplicated lines. A save REPLACES
    the record, so this object IS the template: sending it is sending the whole
    thing, and a field left out is a default cleared. That is visible in the
    document -- `template: { name: "Bug" }` obviously drops the rest, where ten
    sibling arguments would have looked like a patch.

    Optionality here is a wire concern only: every default is nullable in the
    schema because a template with no opinion about a field is the ordinary
    case. There is no three-valued UNSET, deliberately -- see
    `IssueTemplateDraft`.

    `labelIds` defaults to an empty list rather than being nullable. Null and
    "no labels" would be the same thing on a replace, and one spelling of one
    state is fewer than two.
    """

    name: str
    team_id: UUID | None = None
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    estimate: int | None = None
    assignee_id: UUID | None = None
    project_id: UUID | None = None
    cycle_id: UUID | None = None
    label_ids: list[UUID] = strawberry.field(default_factory=list)

    def to_draft(self) -> IssueTemplateDraft:
        """The domain's shape of the same values.

        Converted at the boundary rather than passed through, so nothing below
        this layer holds a Strawberry object -- and so `label_ids` becomes the
        tuple the frozen draft needs. Duplicates are NOT removed here: that is
        a rule about what gets written, and it lives in the service with the
        rest of them.
        """
        return IssueTemplateDraft(
            name=self.name,
            team_id=self.team_id,
            title=self.title,
            description=self.description,
            priority=self.priority,
            estimate=self.estimate,
            assignee_id=self.assignee_id,
            project_id=self.project_id,
            cycle_id=self.cycle_id,
            label_ids=tuple(self.label_ids),
        )


@strawberry.input
class IssueTemplateCreateInput:
    """Save a new template in one workspace.

    `workspaceSlug` and not a workspace id, matching every other slug-taking
    field: a slug is a public string a client may spell, and the server
    resolves it against the caller's memberships.
    """

    workspace_slug: str
    template: IssueTemplateFieldsInput


@strawberry.input
class IssueTemplateUpdateInput:
    """Replace one existing template.

    `id` beside the same fields object the create takes, rather than a patch
    type: a template is edited whole in a form, so clearing a default is
    sending null. See `IssueTemplateFieldsInput`.
    """

    workspace_slug: str
    id: UUID
    template: IssueTemplateFieldsInput


@strawberry.input
class IssueTemplateDeleteInput:
    workspace_slug: str
    id: UUID


@strawberry.input
class IssueCreateFromTemplateInput:
    """File one issue from a template, into one team.

    `teamId` is required even for a team-scoped template, for the reason
    `IssueCreateInput` requires one: an issue belongs to a team, and a server
    that picked would be choosing where work lands by a rule invisible in the
    document. A team-scoped template applied into a different team is refused.

    `title` overrides the template's, for the ordinary case of filing "Deploy
    checklist: 4.2" from a "Deploy checklist" template. Null falls back to the
    template's own title, and a template with neither is refused by the same
    title rule `issueCreate` publishes.

    There is deliberately no way to override anything else. A mutation that
    took the template's id AND every field it fills would not be applying a
    template, it would be `issueCreate` with extra steps -- and the client
    already has that.
    """

    workspace_slug: str
    template_id: UUID
    team_id: UUID
    title: str | None = None
