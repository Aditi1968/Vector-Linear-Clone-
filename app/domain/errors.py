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


class TeamNotFoundError(Exception):
    """A workspace holds no team for work to be filed against.

    Not a client error, and never to be answered as one. Every path that
    raises this has already resolved a real workspace; what is missing is a
    team inside it, which no request can supply and no input can be
    corrected to avoid. Translating it into a validation failure would tell
    a client to fix something on their side that is not theirs to fix, and
    would hide a half-provisioned tenant behind a 200.

    Distinct from WorkspaceNotFoundError, which says the tenant itself did
    not resolve. Collapsing the two would report an unprovisioned workspace
    as a nonexistent one. Pure application code -- no Strawberry, FastAPI,
    asyncpg or PostgreSQL.
    """

    def __init__(self):
        super().__init__("Team not found")
