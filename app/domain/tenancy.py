from dataclasses import dataclass
from uuid import UUID


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
