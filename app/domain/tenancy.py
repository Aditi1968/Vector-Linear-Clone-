from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.domain.errors import WorkspaceAccessDeniedError


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    """The workspace an operation is being performed in.

    This is an identity, not a permission. Holding a WorkspaceScope says
    only which workspace's data an operation addresses. It does not assert
    that the caller was authenticated, that the caller belongs to the
    workspace, or that the operation is allowed. A reader must not take
    "I have a WorkspaceScope" to mean "this caller is allowed to be here":
    none of that has been checked, and this type is not where it will be
    recorded when it is.

    The value is passed explicitly through the calls that need it. There is
    no ambient current workspace, because one process serves many
    workspaces at once, so workspace identity has to travel with the
    operation rather than sit in module or task state.
    """

    workspace_id: UUID


# Every role a workspace membership may carry, least privileged first.
#
# This is the application's copy of `workspace_members_role_check` in
# migrations/004_membership.sql, and the two are pinned equal by
# tests/test_membership_scope.py rather than left to agree by habit. Two
# copies exist because the database has to reject an unknown role whether or
# not the write came through this code, and this code has to know the
# vocabulary without asking the database.
#
# The order is documentation. Nothing may read a privilege comparison out of
# it -- SQL's `IN` has no order, and what a role permits is a decision made
# where the permission is checked, not here.
WORKSPACE_ROLES: Final = ("member", "admin", "owner")

# The roles that may change who else is in the workspace.
#
# A set rather than a slice of the tuple above, because that tuple is
# deliberately not a privilege ordering -- see its note. What a role permits is
# the application's decision, and this is that decision written down once, so
# that "may this caller invite someone" has one answer wherever it is asked.
WORKSPACE_ADMIN_ROLES: Final = frozenset({"admin", "owner"})

# The role a workspace must always have at least one of.
#
# Not merely the most privileged name in WORKSPACE_ROLES: it is the role that
# can grant every other one, so a workspace with none is a tenant nobody can
# ever administer again -- unreachable through the API and recoverable only by
# a hand-written UPDATE. Every removal and demotion is checked against it.
WORKSPACE_OWNER_ROLE: Final = "owner"


@dataclass(frozen=True, slots=True)
class AuthorizedWorkspaceScope(WorkspaceScope):
    """A workspace, the user acting in it, and the role they hold there.

    The difference from its base class is the whole point of having two
    types. A WorkspaceScope is an identity: a slug resolved to a workspace,
    which any caller who can spell the slug can obtain. This says something
    no client can assert about itself -- that `user_id` was found in
    `workspace_members` for `workspace_id`, and that the row said `role`.
    Only a lookup against that table can produce one, which is what makes
    the type worth trusting where the base type is not.

    Subclassing rather than composing is deliberate: tenant-owned work
    already takes a WorkspaceScope, and one of these is a WorkspaceScope, so
    an authorized caller can be handed to it unchanged. The relation only
    holds in that direction, and that asymmetry is the invariant -- a
    function that needs authorization must annotate this type, because a
    WorkspaceScope will satisfy a WorkspaceScope annotation and carries no
    evidence at all.

    Frozen for the same reason the base is, and `frozen=True` is not
    inherited: dataclass arguments are per-class, and Python refuses outright
    to derive a non-frozen dataclass from a frozen one. Were it mutable, a
    caller holding a scope could raise its own role after the check that
    produced it.

    A note for whoever adds the first method here: call up with the explicit
    `super(AuthorizedWorkspaceScope, self)`. Zero-argument `super()` fails
    inside a `slots=True` dataclass, because the decorator returns a *new*
    class object while the compiler's `__class__` cell still points at the
    original one.
    """

    user_id: UUID

    # A plain str, holding one of WORKSPACE_ROLES. Not an enum, because the
    # value's authority comes from the database CHECK that admitted the row
    # and not from a type this process defines -- and every consumer either
    # compares it to a known role or hands it to a transport that has its own
    # vocabulary. The GraphQL enum is that transport's copy; it converts at
    # the boundary and raises on a role it does not know.
    role: str


def require_workspace_admin(scope: AuthorizedWorkspaceScope) -> None:
    """Refuse a caller who may act in the workspace but not administer it.

    One function rather than a comparison written out in each service, for the
    reason `app.graphql.viewer.viewer_user_id` gives about identity: two copies
    of a permission check are two things that can drift, and the one that
    drifts is the one nobody re-read.

    Raises WorkspaceAccessDeniedError, the same exception a non-member gets,
    so the transport has one refusal to translate and answers both with the
    same NOT_FOUND. That costs an ordinary member a precise message and buys
    a property worth more: the response to "list this workspace's invitations"
    does not distinguish a workspace the caller cannot administer from one
    they cannot see at all.
    """
    if scope.role not in WORKSPACE_ADMIN_ROLES:
        raise WorkspaceAccessDeniedError()
