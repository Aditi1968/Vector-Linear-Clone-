from datetime import date
from uuid import UUID

import strawberry

from app.domain.recurrence import RecurrenceFrequency, RecurrenceRule
from app.domain.templates import IssueTemplateDraft

# Imported for its side effect, not for the name -- `types/template.py` is
# where `strawberry.enum` annotates RecurrenceFrequency with its GraphQL
# definition, and a field referencing the bare class before that has run makes
# Strawberry mint a SECOND definition for the same name, at which point the
# schema refuses to build. The same import `inputs/issue.py` carries for
# WorkflowStateCategory.
from app.graphql.types.template import RecurrenceFrequencyType  # noqa: F401


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
class IssueRecurrenceSetInput:
    """Make one template file itself on a schedule, replacing any it had.

    An upsert and not a create/update pair, because there is at most one
    schedule per template and the product operation is "this template recurs
    like THIS" -- the same request whether or not it recurred before. Two
    mutations differing only in whether a row happens to exist would put that
    question on the client.

    `teamId` is required even for a team-scoped template, for the reason
    `IssueCreateFromTemplateInput` requires one plus one more: the sweep that
    files these issues runs on no request and has no caller to ask which team,
    so the answer has to be written down when the schedule is. A team-scoped
    template scheduled into a different team is refused.

    `weekdays` and `dayOfMonth` belong to exactly one frequency each and are
    refused with the other two -- a WEEKLY schedule naming no weekday would
    never fire, and a DAILY one carrying weekdays is a row two readers would
    disagree about. Send the one your frequency uses and omit the other.

    There is deliberately no `nextRunOn`. It is computed from the rule, because
    a client able to name it could file an issue immediately by backdating it,
    and could name a day the schedule does not fall on -- which every later
    advance would then walk away from.

    There is deliberately no `endsOn` and no pause switch either. Deleting the
    schedule is how a recurrence stops, which is one mutation instead of two
    ways to say the same thing; a workspace that wants to pause a recurrence
    for a month clears it and sets it again, which is exactly as many clicks.
    """

    workspace_slug: str
    template_id: UUID
    team_id: UUID

    frequency: RecurrenceFrequency
    starts_on: date

    # Defaulted to 1, which is what "weekly" means without qualification, so a
    # client sending `{frequency: WEEKLY, weekdays: [1]}` gets every Monday
    # rather than an error about a field it had no opinion about.
    interval_count: int = 1

    # Both nullable and both defaulted, because each is required by exactly one
    # frequency. `weekdays` defaults to an empty list rather than null: null and
    # "no weekdays" would be the same thing here, and one spelling of one state
    # is fewer than two.
    weekdays: list[int] = strawberry.field(default_factory=list)
    day_of_month: int | None = None
    due_in_days: int | None = None

    def to_rule(self) -> RecurrenceRule:
        """The domain's shape of the same values.

        Converted at the boundary rather than passed through, so nothing below
        this layer holds a Strawberry object.

        The weekdays are SORTED AND DEDUPLICATED here rather than refused. A
        client sending Monday twice is not making a mistake worth an error; it
        is sending a set spelled carelessly, and
        `issue_recurrences_weekdays_valid` bounds the array at seven elements --
        which a repeated Monday could otherwise exceed while naming three days.
        Sorting also makes the stored value canonical, so two saves of the same
        schedule are the same row rather than two spellings of it.

        The RANGE of each weekday is not checked here: `TemplateService`
        publishes that as a field error, alongside every other rule about this
        input, so there is one place a client's schedule is judged.
        """
        return RecurrenceRule(
            frequency=self.frequency,
            interval_count=self.interval_count,
            starts_on=self.starts_on,
            weekdays=tuple(sorted(set(self.weekdays))),
            day_of_month=self.day_of_month,
            due_in_days=self.due_in_days,
        )


@strawberry.input
class IssueRecurrenceClearInput:
    """Stop one template recurring.

    The template id and not a recurrence id, because a schedule has no identity
    of its own: `issue_recurrences_pkey` is the template, and there is at most
    one.
    """

    workspace_slug: str
    template_id: UUID


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
