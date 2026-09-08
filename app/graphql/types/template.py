"""Transport types for the shapes a new issue can be filed from."""

from datetime import date, datetime
from uuid import UUID

import strawberry

from app.domain.recurrence import RecurrenceEntity, RecurrenceFrequency
from app.domain.templates import IssueTemplateEntity
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueType


# The domain enum, published rather than restated -- the same move
# `types/team.py` makes for WorkflowStateCategory, and for the same reason:
# two enums of strings that must agree drift in a way that type-checks.
RecurrenceFrequencyType = strawberry.enum(
    RecurrenceFrequency,
    name="RecurrenceFrequency",
    description=(
        "How often a template files itself. Deliberately three words and not a "
        "cron expression: a cron field is a small language with its own parser "
        "and its own surprises, bought so a project tracker can express a "
        "schedule nobody asks a project tracker for."
    ),
)


@strawberry.type(
    name="IssueRecurrence",
    description="The schedule on which a template files itself.",
)
class IssueRecurrenceType:
    """One template's schedule, as a client reads it back.

    `weekdays` is empty and `dayOfMonth` null for the frequencies that do not
    use them, which is not a client's problem to remember: WEEKLY always
    carries weekdays and MONTHLY always carries a day, because
    `issue_recurrences_weekdays_match_frequency` and its sibling make those
    equivalences structural rather than conventional.

    `nextRunOn` is the one field that is not a copy of what was saved -- it is
    the scheduler's own state, and it is published so a settings screen can say
    when the next issue arrives without recomputing the calendar and risking a
    different answer from the sweep's. It moves forward when the sweep files an
    issue, and RESETS to the first occurrence of the new rule whenever the
    schedule is edited.

    There is deliberately no `lastRunOn` and no link to the issues this has
    already filed. An issue filed from a template is an ordinary issue
    afterwards -- the same rule that makes deleting a template leave its issues
    alone -- and a field claiming otherwise would be a second, weaker answer to
    "what did this produce" than the board already gives.
    """

    frequency: RecurrenceFrequency
    interval_count: int = strawberry.field(
        description="The N in 'every N days / weeks / months'. At least 1."
    )
    weekdays: list[int] = strawberry.field(
        description=(
            "ISO weekday numbers, 1 = Monday through 7 = Sunday. Non-empty for "
            "WEEKLY and empty for the other two."
        )
    )
    day_of_month: int | None = strawberry.field(
        description=(
            "1-31 for MONTHLY, null otherwise. The 31st is CLAMPED to the last "
            "day of a shorter month rather than skipping it -- and the clamp is "
            "applied to this number every month rather than to the previous "
            "instance, so February lands on the 28th and March returns to the "
            "31st."
        )
    )
    starts_on: date = strawberry.field(
        description=(
            "The day the schedule is anchored to and the first it may fire. "
            "Load-bearing above an interval of 1: 'every two weeks on Tuesday' "
            "does not say which Tuesdays without a week to count from."
        )
    )
    due_in_days: int | None = strawberry.field(
        description=(
            "The generated issue's due date, as days after the day it is "
            "filed. Null for a recurring issue with no due date. Counted from "
            "the filing day and not from the scheduled one, so an issue filed "
            "late by a sweep that was down is not born overdue."
        )
    )
    next_run_on: date = strawberry.field(
        description="The day the next issue will be filed."
    )
    team_id: UUID = strawberry.field(
        description=(
            "Which team the generated issue lands in. Required even for a "
            "workspace-wide template, because an issue belongs to a team and "
            "the sweep that files it has no caller to ask."
        )
    )

    @classmethod
    def from_entity(cls, entity: RecurrenceEntity) -> "IssueRecurrenceType":
        return cls(
            frequency=entity.frequency,
            interval_count=entity.interval_count,
            # A list on the wire, a tuple in the domain -- the boundary doing
            # its job, exactly as `label_ids` below.
            weekdays=list(entity.weekdays),
            day_of_month=entity.day_of_month,
            starts_on=entity.starts_on,
            due_in_days=entity.due_in_days,
            next_run_on=entity.next_run_on,
            team_id=entity.team_id,
        )


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
    recurrence: IssueRecurrenceType | None = strawberry.field(
        description=(
            "The schedule this template files itself on, or null for one "
            "somebody applies by hand -- which is nearly all of them. Set and "
            "cleared by their own mutations rather than by the save above: a "
            "save REPLACES the template, so a schedule carried in that input "
            "would be cleared every time somebody fixed a typo in the title."
        )
    )
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
            recurrence=(
                None
                if entity.recurrence is None
                else IssueRecurrenceType.from_entity(entity.recurrence)
            ),
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
class IssueRecurrenceSetPayload:
    """The schedule as it now is.

    The recurrence and not the whole template, because that is what changed --
    and because returning the template would invite a client to re-render a
    menu entry from a payload that answered a different question. The template
    is unchanged; a client that holds it already holds it.
    """

    recurrence: IssueRecurrenceType | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueRecurrenceClearPayload:
    """Whether a schedule was there to stop.

    `cleared` is False for a template that was not recurring and for one in
    another workspace alike -- both are "the state you asked for holds", which
    is what makes clearing idempotent and keeps a template's existence
    unobservable. A client renders the same thing either way; the boolean is
    there so a retry after a dropped response can tell that it was the retry.
    """

    cleared: bool
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
