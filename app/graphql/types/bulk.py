"""Transport types for bulk actions.

One payload for both mutations, and it carries `IssueSummary` rather than
`Issue`. That is the same acyclicity argument `app.graphql.types.relations`
makes -- nothing reachable from a summary returns an issue -- with a second
reason on top that is specific to this feature: a bulk payload holds up to
`BULK_MAX` issues, so every field on the type is charged a hundred times by
`app.graphql.limits`. `Issue` selects its labels, its project, its cycle and
its relations, and a client that asked for those over a hundred rows would be
buying a hundred fan-outs from a mutation that has already done its work.

The client already knows which issues it selected. What it needs back is what
changed on them.
"""

import strawberry

from app.domain.issues import IssueEntity
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.relations import IssueSummaryType


@strawberry.type
class IssueBulkPayload:
    """What the batch did, or why it did nothing.

    `issues` is empty exactly when `errors` is not. There is no partial
    outcome to report and no third case to handle, because the service runs
    every write in one transaction and any refusal -- including an id this
    workspace does not hold -- rolls the whole thing back. A client reading
    this can take a non-empty `errors` to mean that every issue it named is
    untouched.

    `count` is the number of issues actually changed, which is the number a
    toast renders ("12 issues updated"). It is redundant with `len(issues)` and
    is here anyway, because the alternative is every client computing it and
    one of them computing it from the ids it SENT -- which is the number it
    asked for rather than the number that moved.
    """

    issues: list[IssueSummaryType]
    count: int
    errors: list[ValidationErrorType]

    @classmethod
    def of(cls, entities: list[IssueEntity]) -> "IssueBulkPayload":
        return cls(
            issues=[IssueSummaryType.from_entity(entity) for entity in entities],
            count=len(entities),
            errors=[],
        )

    @classmethod
    def refused(cls, errors: list[ValidationErrorType]) -> "IssueBulkPayload":
        """Nothing happened, and here is why.

        `count` is 0 rather than absent, so a client rendering the number does
        not have to branch on the error case to avoid printing null.
        """
        return cls(issues=[], count=0, errors=errors)
