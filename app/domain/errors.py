from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    field: str
    code: str
    message: str


class ValidationError(Exception):
    """An expected input validation failure.

    The structured `issues` collection is the contract; the exception
    message stays generic on purpose so that nothing downstream is tempted
    to parse it. Pure application code -- no Strawberry, FastAPI, asyncpg
    or PostgreSQL.
    """

    def __init__(self, issues: list[ValidationIssue]):
        super().__init__("Validation failed")

        self.issues = issues


class WorkspaceNotFoundError(Exception):
    """A workspace slug did not resolve to a workspace.

    That is the whole meaning: the slug matched nothing. It never says a
    caller was refused a workspace. The two answers must stay distinct --
    reporting a miss as a refusal implies the workspace exists, and
    reporting a refusal as a miss hides that a check failed.

    The slug is deliberately not carried. Whoever raises this already holds
    it and can log it there, still in the context that knows how to bound
    and escape it; attaching an unsanitised client string to an exception
    that travels up the stack and into logs adds no information and invites
    it into a message a client can see. Pure application code -- no
    Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    def __init__(self):
        super().__init__("Workspace not found")
