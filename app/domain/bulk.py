"""Bulk actions: one change applied to many issues, or to none of them.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.

Two properties define this module, and neither is about convenience.

ALL OR NOTHING. A bulk action is one act, not N acts that happen to share a
request. Half of a "move these twelve to Done" is a state nobody asked for and
that no client can repair without knowing which half landed, so every statement
runs inside one transaction and any refusal takes the whole batch down with it.
That includes the refusals that are not errors in the database's eyes: an id
that names nothing in the caller's workspace updates no row, which PostgreSQL
reports as success, so the service counts what came back and fails the batch
when the count disagrees with what was asked for.

BOUNDED. The id list arrives from a browser and every id in it is
attacker-controlled. An unbounded list is a way to ask one request to hold a
row lock on every issue in a workspace, so `BULK_MAX` is a refusal and not a
truncation -- a batch that is too large is rejected with a field error rather
than silently applied to its first hundred entries, which would be a mutation
the client did not ask for and cannot see.
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.domain.issues import UNSET, Unset


# The most issues one bulk action may name.
#
# A ceiling, not a page size. The list is not paginated -- a client sends the
# ids it selected -- so this is the number above which the request is refused.
#
# 100 rather than a larger round number, and the reasoning is about locks
# rather than about bytes. Every issue in the batch is locked FOR UPDATE for
# the life of the transaction, so the batch size is also the number of issues
# that cannot be edited by anyone else while it runs. A hundred is more than
# any list view shows at once and small enough that the lock is held for
# milliseconds.
#
# It is deliberately NOT tied to `app.graphql.limits.ASSUMED_PAGE_SIZE`, which
# happens to be the same number: that one prices a READ the complexity rule
# cannot see the size of, and this one bounds a WRITE. Two limits that agree by
# coincidence must not become one limit by accident.
BULK_MAX = 100

BULK_MIN = 1


@dataclass(frozen=True, slots=True)
class BulkIssuePatch:
    """The fields a bulk action sets, with everything else left alone.

    Every field defaults to UNSET, so a patch that names nothing changes
    nothing -- the same three-state distinction `IssuePatch` needs and for the
    same reason: "set the assignee to nobody" and "do not touch the assignee"
    are different requests and a plain `None` cannot tell them apart.

    Deliberately NOT `IssuePatch`. The two overlap on four fields and differ on
    what they may carry, and the differences are the interesting part:

      * no `title` and no `description`. Setting one title on many issues is
        not an edit anybody wants, and offering it would make the obvious
        mistake -- selecting a page and typing a title -- destroy work
        irreversibly.
      * `project_id` with `milestone_id`, and `cycle_id`, which `IssuePatch`
        omits because the single-issue paths are `set_project` and `set_cycle`.
        A bulk action has no such split: a client moving twelve issues into a
        project and a cycle is doing one thing, and three round trips would be
        three transactions with two ways to end up half done.

    `completed_at` is absent and there is no way to add it, exactly as in
    `IssuePatch`: it is derived from the resulting workflow state, so a caller
    able to set it could put the two into a disagreement no later write
    repairs.

    `archived_at` is absent for the other reason `IssuePatch` gives -- archiving
    is its own operation with its own authorisation story, and it is a separate
    service method here too.
    """

    workflow_state_id: UUID | None | Unset = UNSET
    assignee_id: UUID | None | Unset = UNSET
    priority: int | None | Unset = UNSET
    estimate: int | None | Unset = UNSET
    due_date: date | None | Unset = UNSET

    # Written together, never separately. `issues_milestone_requires_project`
    # refuses a milestone with no project and `issues_milestone_fk` refuses a
    # milestone belonging to a different project than the row's own, so a patch
    # that could name one without the other would have to pass through a state
    # the schema forbids in order to reach one it allows. `IssueService.
    # set_project` makes the same argument about the single-issue path.
    project_id: UUID | None | Unset = UNSET
    milestone_id: UUID | None | Unset = UNSET

    cycle_id: UUID | None | Unset = UNSET

    @property
    def is_empty(self) -> bool:
        """Whether this patch would change no column at all.

        A bulk request may still be non-empty with an empty patch -- attaching
        a label changes no column on `issues` -- so this is one input to that
        decision and not the whole of it. `BulkService` makes the decision.
        """
        return all(
            value is UNSET
            for value in (
                self.workflow_state_id,
                self.assignee_id,
                self.priority,
                self.estimate,
                self.due_date,
                self.project_id,
                self.milestone_id,
                self.cycle_id,
            )
        )


# The wide patch, as a shared instance.
#
# Frozen, so one instance is safe -- and a default argument has to be a
# singleton anyway: `def update_many(..., patch = BulkIssuePatch())` builds one
# at import time and hands the SAME object to every caller regardless, which is
# the bug ruff's B008 is about. `app.domain.issues.NO_FILTER` is the same move.
NO_CHANGES = BulkIssuePatch()
