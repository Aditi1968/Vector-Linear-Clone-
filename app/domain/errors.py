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


class AuthenticationError(Exception):
    """A login attempt did not establish an identity.

    One exception for every way that can happen: no account with that
    address, and the wrong password for an account that exists. They are
    deliberately indistinguishable, because a caller who can tell them apart
    can enumerate which addresses have accounts here by reading error codes,
    and an address is not the kind of thing this service gets to disclose.

    That indistinguishability is a property of the whole login path, not of
    this class -- the timing has to match too, which is AuthService's job.
    Kept separate from ValidationError because the two say different things:
    ValidationError means the input was malformed and can be corrected by
    looking at it, this means the input was well-formed and wrong. Folding
    them together would put "your password is incorrect" in the same channel
    as "your password is too long", and only one of those may say why.

    Carries no email, no user id and no detail. Pure application code -- no
    Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    def __init__(self):
        super().__init__("Authentication failed")


class EmailAlreadyRegisteredError(Exception):
    """A registration lost the race for an address that is already taken.

    Raised by the repository, because "taken" is a fact only the database
    holds and only its unique constraint can decide without a window between
    the check and the insert. Translated into a ValidationError by the
    service, because that is the layer that decides what a client is told.

    The two-step exists so that no asyncpg exception has to travel upward to
    be interpreted. A UniqueViolationError says nothing on its own -- it
    could be any constraint on any table -- and reading its constraint name
    is SQL knowledge, which belongs with the SQL.

    Carries no address, for the same reason WorkspaceNotFoundError carries no
    slug. Pure application code -- no Strawberry, FastAPI, asyncpg or
    PostgreSQL.
    """

    def __init__(self):
        super().__init__("Email already registered")


class TeamNotFoundError(Exception):
    """A team id did not resolve to a team in the workspace it was used in.

    Deliberately one error for two situations that must stay externally
    indistinguishable: the team does not exist at all, and the team exists
    in some other workspace. Separating them would answer "does workspace B
    have a team with this id?" for any caller holding an id and a workspace
    they can reach, which is exactly the cross-tenant existence check
    tenancy is meant to deny.

    Neither the id nor the workspace is carried, for the reason
    `WorkspaceNotFoundError` gives: whoever raises this holds both already
    and can log them in the frame that knows how to bound them. Pure
    application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    def __init__(self):
        super().__init__("Team not found")


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
