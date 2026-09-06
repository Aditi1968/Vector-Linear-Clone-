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


class WorkspaceAccessDeniedError(Exception):
    """A (slug, user) pair yields no workspace the user may act in.

    Raised identically for a slug that matches no workspace and for a slug
    that matches a workspace the user is not a member of. That is not a
    convenience: telling the two apart tells an unauthenticated stranger
    which workspace slugs exist, which is enough to enumerate a customer
    list one guess at a time.

    The indistinguishability is structural rather than a decision taken on
    the way out. `MembershipRepository.find_membership` answers both cases
    with the same absent row from a single statement, so no frame in the
    server ever computes the difference -- there is nothing for a later
    refactor to leak into a message, a log line, or a response that comes
    back measurably faster. Contrast WorkspaceNotFoundError above, which is
    raised by a lookup that deliberately answers existence and nothing else,
    for callers who are entitled to that answer.

    Neither the slug nor the user id is carried, for the reason given on
    WorkspaceNotFoundError: whoever raises this holds both and can log them
    in the frame that knows how to bound and escape them. Pure application
    code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    def __init__(self):
        super().__init__("Workspace is not accessible")
