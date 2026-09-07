from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.issues import IssueEntity
from app.domain.templates import IssueTemplateDraft, IssueTemplateEntity
from app.domain.tenancy import WorkspaceScope
from app.repositories.templates import TemplateRepository


if TYPE_CHECKING:
    from app.services.issues import IssueService
    from app.services.labels import LabelService


NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 100

# The issue-shaped bounds, matching what `IssueService` and the schema already
# accept. A template that could hold a title `issueCreate` refuses would be a
# template that saves and never works.
TITLE_MAX_LENGTH = 500
DESCRIPTION_MAX_LENGTH = 16384

PRIORITY_MIN = 0
PRIORITY_MAX = 4

ESTIMATE_MIN = 0

# The most templates one listing returns.
#
# ponytail: a hard cap and no cursor, because templates are a workspace's
# CONFIGURATION -- a menu somebody maintains by hand -- rather than a product
# list that grows with use. A workspace with more than this many would see a
# silently truncated menu; when one exists, this becomes a keyset page over
# (name, id), which `issue_templates_workspace_name_idx` already serves in that
# exact order.
TEMPLATES_MAX = 200

# The most labels one template may carry, matching `LABELS_PER_ISSUE_MAX`.
#
# Imported rather than redeclared would be the obvious move and is the wrong
# one: this is a bound on a TEMPLATE, and the reason it equals the issue's is
# that a template applying more labels than an issue may wear is a template
# whose apply always fails halfway. Writing it here says that out loud, and
# `tests/test_templates.py` pins the two equal so they cannot drift apart
# silently.
LABELS_PER_TEMPLATE_MAX = 50


# Foreign keys whose violation is a client mistake, and the field error each
# one becomes. The same shape, and the same reasoning, as
# `IssueService._EXPECTED_FOREIGN_KEYS`: discriminating on the constraint name
# is what keeps this from turning unexpected database errors into validation
# errors, because each name identifies exactly one rule about one value the
# client supplied.
#
# None of them distinguishes "belongs to another workspace" from "does not
# exist", and that is the point rather than a limitation: telling them apart
# would confirm the existence of a resource the caller cannot see. A template
# is where that matters most -- it is the one place a client can park an id and
# come back to it later.
#
# `issue_templates_workspace_fk` is absent deliberately. Its value comes from
# the authorized scope rather than from the request, so violating it is a
# defect here and not a correction for the client to make.
_EXPECTED_FOREIGN_KEYS: dict[str, ValidationIssue] = {
    "issue_templates_team_fk": ValidationIssue(
        field="teamId",
        code="NOT_FOUND",
        message="Team not found",
    ),
    "issue_templates_assignee_fk": ValidationIssue(
        field="assigneeId",
        code="NOT_A_MEMBER",
        message="Assignee must be a member of this workspace",
    ),
    "issue_templates_project_fk": ValidationIssue(
        field="projectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    "issue_templates_cycle_fk": ValidationIssue(
        field="cycleId",
        code="NOT_FOUND",
        message="Cycle not found in this team",
    ),
    "issue_template_labels_label_fk": ValidationIssue(
        field="labelIds",
        code="NOT_FOUND",
        message="Label not found",
    ),
}

TEMPLATE_NOT_FOUND = ValidationIssue(
    field="templateId",
    code="NOT_FOUND",
    message="Template not found",
)


class TemplateService:
    """Business rules for issue templates, and for filing an issue from one.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service also
    owns connection acquisition and transaction boundaries.

    The workspace is threaded through as an argument on every method rather
    than held on the instance; see `IssueService` and `WorkspaceScope`. Holding
    a scope is not permission to act in it.

    WHAT MAKES A STORED TEMPLATE SAFE
    ---------------------------------
    A template is the only thing in this product that is attacker-controlled
    data NAMING OTHER ROWS, written once and replayed later by other people. So
    it gets checked twice, by two different mechanisms, and neither is a
    re-reading of the other:

    * On SAVE, by the composite foreign keys migration 020 declares. A
      template in workspace A cannot hold workspace B's team, member, project,
      cycle or label, because there is one workspace_id column and every key
      reads it. This service does not look any of them up first -- that would
      be a second, weaker copy of five rules, weaker because it can be raced.

    * On APPLY, by filing the issue through `IssueService` and `LabelService`
      under the APPLYING caller's scope. Every id the template carries is
      handed to `issues_assignee_fk`, `issues_project_fk`, `issues_cycle_fk`
      and `issue_labels_label_fk`, which check it against the workspace of the
      issue being created rather than against anything the template said. The
      template is read under the caller's scope too, so a template id from
      another tenant resolves to nothing and no defaults are ever in hand to
      re-validate.

    The second check is not redundant with the first. It is what makes the
    apply path correct without depending on the save path having been correct
    -- which is the property that survives a future bulk import, a data fix
    applied by hand, or a sixth column somebody adds without a foreign key.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: TemplateRepository,
        issues: "IssueService",
        labels: "LabelService",
    ):
        self._pool = pool
        self._repository = repository

        # Applying a template files an issue and puts labels on it, and both
        # of those are rules that live somewhere already: which state a new
        # issue starts in, which number it gets, how many labels one issue may
        # wear. Collaborators are services rather than repositories because
        # every one of those rules is a service's, and reaching for the
        # repositories instead would be this file quietly growing a second
        # `issueCreate` that nobody would think to keep in step.
        self._issues = issues
        self._labels = labels

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        draft: IssueTemplateDraft,
    ) -> IssueTemplateEntity:
        """Save one new template in this workspace."""
        draft = self._validated(draft)

        async with self._pool.acquire() as connection:
            # The template row and its label rows are two writes and one
            # meaning: a template that exists carrying none of the labels the
            # save asked for is not the template anybody saved.
            try:
                async with connection.transaction():
                    return await self._repository.create(
                        connection,
                        scope=scope,
                        draft=draft,
                    )
            except asyncpg.ForeignKeyViolationError as exc:
                # Caught outside the transaction block so the rollback has
                # already happened by the time this runs.
                error = _validation_error_for(exc.constraint_name)

                if error is None:
                    raise

                raise error from None

    async def update(
        self,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
        draft: IssueTemplateDraft,
    ) -> IssueTemplateEntity:
        """Replace one template wholesale, or report that it is not here.

        A replace and not a patch, for the reason `IssueTemplateDraft` gives:
        a template is a small record edited whole in a form, so clearing a
        default is sending null rather than a third state in every field.

        "Not in this workspace" and "does not exist" are one answer, because
        the one the caller is not entitled to must not be distinguishable from
        the one that is ordinary.
        """
        draft = self._validated(draft)

        async with self._pool.acquire() as connection:
            try:
                async with connection.transaction():
                    entity = await self._repository.update(
                        connection,
                        scope=scope,
                        template_id=template_id,
                        draft=draft,
                    )
            except asyncpg.ForeignKeyViolationError as exc:
                error = _validation_error_for(exc.constraint_name)

                if error is None:
                    raise

                raise error from None

        if entity is None:
            raise ValidationError([TEMPLATE_NOT_FOUND])

        return entity

    async def delete(
        self,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
    ) -> UUID:
        """Discard one template, returning the id that went.

        The label rows go first and in the same transaction; see
        `TemplateRepository.delete` for why the RESTRICT that forces that
        ordering is the wanted behaviour rather than an obstacle.

        Deleting a template does NOT touch the issues filed from it. There is
        no link between them by design: a template is a starting shape, and an
        issue that was once filed from one is an ordinary issue afterwards.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                deleted = await self._repository.delete(
                    connection,
                    scope=scope,
                    template_id=template_id,
                )

        if not deleted:
            raise ValidationError([TEMPLATE_NOT_FOUND])

        return template_id

    async def get(
        self,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
    ) -> IssueTemplateEntity | None:
        """One template from this workspace, or nothing.

        A single SELECT needs no explicit write transaction, so this acquires
        a connection without opening one.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get(
                connection,
                scope=scope,
                template_id=template_id,
            )

    async def list(
        self,
        *,
        scope: WorkspaceScope,
        team_id: UUID | None,
    ) -> list[IssueTemplateEntity]:
        """The templates one team may file from, in menu order.

        The workspace's shared templates plus that team's own, capped at
        TEMPLATES_MAX and without a cursor -- see that constant for why a
        configuration list gets a ceiling where a product list gets a page.

        `team_id` is not validated against the workspace. A team id from
        another tenant selects no team-scoped rows, so the answer is the shared
        templates -- the same answer a real team with none of its own gets --
        and a service that looked the team up first and raised would be
        reporting that another tenant's team is real.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list_for_team(
                connection,
                scope=scope,
                team_id=team_id,
                limit=TEMPLATES_MAX,
            )

    async def apply(
        self,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
        team_id: UUID,
        title: str | None = None,
        actor_id: UUID | None = None,
    ) -> IssueEntity:
        """File one issue from a template, into a team of this workspace.

        `team_id` is a required argument even for a team-scoped template, for
        the reason `IssueService.create` gives about not defaulting one: the
        caller that knows which team it means is the one that has to say so,
        and a service that picked would be choosing where another tenant's work
        lands by a rule invisible at the call site. A team-scoped template
        applied into a DIFFERENT team is refused here rather than left to
        `issues_cycle_fk` to refuse obscurely later -- both values are already
        in hand, so it is a property of the arguments alone.

        `title` overrides the template's. A template with neither is refused by
        `IssueService`'s own title rule rather than by a copy of it here, which
        is what keeps one answer to "what is a valid title".

        NOT ONE TRANSACTION
        -------------------
        ponytail: the issue, its project, its cycle and its labels are four
        calls across two services, each of which owns a transaction of its own.
        A failure part way through leaves a well-formed issue missing some of
        the template's defaults -- visible, editable, and not corrupt -- rather
        than nothing at all. Making it atomic means a connection-taking
        `IssueService.create`, which is a change to the most load-bearing write
        in the product and belongs in its own commit. Do that before this path
        grows a fifth step.

        The order is deliberate anyway: the issue first, so a failure later
        leaves the work recorded, and the labels last, because they are the
        cheapest thing for a person to redo.
        """
        template = await self.get(scope=scope, template_id=template_id)

        if template is None:
            raise ValidationError([TEMPLATE_NOT_FOUND])

        if template.team_id is not None and template.team_id != team_id:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="teamId",
                        code="TEAM_MISMATCH",
                        message="This template belongs to a different team",
                    )
                ]
            )

        issue = await self._issues.create(
            scope=scope,
            team_id=team_id,
            # `or ""` rather than a check here: an empty title reaches
            # `IssueService._validate_create` and comes back as the REQUIRED
            # error that field already publishes.
            title=title if title is not None else (template.title or ""),
            description=template.description,
            # `priority` is NOT NULL on the issue with a default of 0, and a
            # template with no opinion is asking for that default rather than
            # for a null the column cannot hold.
            priority=template.priority if template.priority is not None else 0,
            assignee_id=template.assignee_id,
            creator_id=actor_id,
            estimate=template.estimate,
        )

        if template.project_id is not None:
            issue = await self._issues.set_project(
                scope=scope,
                issue_id=issue.id,
                project_id=template.project_id,
                # A template carries a project and never a milestone: a
                # milestone is a position INSIDE a project's plan, and a shape
                # filed months later has no business claiming one.
                milestone_id=None,
                actor_id=actor_id,
            )

        if template.cycle_id is not None:
            issue = await self._issues.set_cycle(
                scope=scope,
                issue_id=issue.id,
                cycle_id=template.cycle_id,
                actor_id=actor_id,
            )

        for label_id in template.label_ids:
            await self._labels.attach(
                scope=scope,
                issue_id=issue.id,
                label_id=label_id,
                actor_id=actor_id,
            )

        return issue

    @staticmethod
    def _validated(draft: IssueTemplateDraft) -> IssueTemplateDraft:
        """Every field rule, then the draft the write should use.

        Returns a draft rather than mutating one, because
        `IssueTemplateDraft` is frozen -- and because the one normalisation
        that happens here, deduplicating the labels, changes what is written.
        A caller sending the same label twice is not making a mistake worth
        an error; it is sending a set spelled carelessly, and
        `issue_template_labels_pkey` would otherwise turn that into a unique
        violation with no field attached.

        Field order is deterministic and matches the input type's, so that
        clients can rely on it; the codes and messages are a public contract.
        Every violation is collected before anything is raised, so a form with
        three bad fields reports three.
        """
        label_ids = tuple(sorted(set(draft.label_ids)))

        issues: list[ValidationIssue] = []

        if not NAME_MIN_LENGTH <= len(draft.name) <= NAME_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="OUT_OF_RANGE",
                    message=(
                        f"Name must be between {NAME_MIN_LENGTH} and "
                        f"{NAME_MAX_LENGTH} characters"
                    ),
                )
            )

        if draft.title is not None and len(draft.title) > TITLE_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="title",
                    code="TOO_LONG",
                    message=f"Title must be at most {TITLE_MAX_LENGTH} characters",
                )
            )

        if (
            draft.description is not None
            and len(draft.description) > DESCRIPTION_MAX_LENGTH
        ):
            issues.append(
                ValidationIssue(
                    field="description",
                    code="TOO_LONG",
                    message=(
                        f"Description must be at most {DESCRIPTION_MAX_LENGTH} "
                        "characters"
                    ),
                )
            )

        if draft.priority is not None and not (
            PRIORITY_MIN <= draft.priority <= PRIORITY_MAX
        ):
            issues.append(
                ValidationIssue(
                    field="priority",
                    code="OUT_OF_RANGE",
                    message=f"Priority must be between {PRIORITY_MIN} and {PRIORITY_MAX}",
                )
            )

        if draft.estimate is not None and draft.estimate < ESTIMATE_MIN:
            issues.append(
                ValidationIssue(
                    field="estimate",
                    code="OUT_OF_RANGE",
                    message=f"Estimate must be {ESTIMATE_MIN} or greater",
                )
            )

        # The one rule the arguments alone decide, and the reason it is here
        # rather than left to `issue_templates_cycle_requires_team`: a cycle
        # belongs to a team, so asking for one without a team is not a lookup
        # that might succeed, it is a request no state of the database can
        # satisfy. Rejecting it here means no connection is acquired for it.
        # The CHECK stays as the floor -- if it ever fires, this rule and it
        # disagree, and that is a defect to surface rather than input to
        # report.
        if draft.cycle_id is not None and draft.team_id is None:
            issues.append(
                ValidationIssue(
                    field="cycleId",
                    code="TEAM_REQUIRED",
                    message="A cycle can only be set on a team's template",
                )
            )

        if len(label_ids) > LABELS_PER_TEMPLATE_MAX:
            issues.append(
                ValidationIssue(
                    field="labelIds",
                    code="LIMIT_EXCEEDED",
                    message=(
                        f"A template may carry at most {LABELS_PER_TEMPLATE_MAX} labels"
                    ),
                )
            )

        if issues:
            raise ValidationError(issues)

        return IssueTemplateDraft(
            name=draft.name,
            team_id=draft.team_id,
            title=draft.title,
            description=draft.description,
            priority=draft.priority,
            estimate=draft.estimate,
            assignee_id=draft.assignee_id,
            project_id=draft.project_id,
            cycle_id=draft.cycle_id,
            label_ids=label_ids,
        )


def _validation_error_for(constraint_name: str | None) -> ValidationError | None:
    """The field error this foreign key stands for, or None for the rest.

    Takes the constraint's name rather than the exception, so nothing about
    asyncpg reaches this function -- and so the caller keeps the original
    exception in hand for the None case, where a bare `raise` re-raises it with
    its traceback intact. A constraint absent from the mapping is not a rule
    about client input, and is left to be masked and logged with every other
    unexpected failure. The same split `IssueService` makes, spelled the same
    way so the two read alike.
    """
    return (
        ValidationError([issue])
        if (issue := _EXPECTED_FOREIGN_KEYS.get(constraint_name or "")) is not None
        else None
    )
