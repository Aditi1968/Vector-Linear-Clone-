from uuid import UUID

import strawberry
from strawberry.scalars import JSON


# Every update input below spells its optional fields `T | None =
# strawberry.UNSET`, and the two halves mean different things -- the argument
# app/graphql/inputs/project.py makes in full. `strawberry.UNSET` as the default
# is what makes an omitted field arrive as UNSET rather than as None, which is
# the only way a partial update can tell "leave this alone" from "set this to
# null".


@strawberry.input
class DocumentCreateInput:
    # A slug and not a workspace id, deliberately. CLAUDE.md forbids trusting a
    # workspace id from the frontend: the slug is a public string that selects
    # WHAT is being asked about, and `app.graphql.scope` decides whether the
    # session behind the request may act there.
    workspace_slug: str

    title: str

    # The ProseMirror/TipTap tree, as arbitrary JSON.
    #
    # `JSON` and not a nest of input types, and this is the one place that
    # decision could be argued the other way. A GraphQL input type per node kind
    # cannot express a recursive union at all -- input types have no
    # interfaces and no unions -- so the schema would have to be a single
    # `DocumentNodeInput` with every attribute of every node on it, all
    # nullable, which validates strictly less than nothing. The real validation
    # is `app.domain.documents.parse_content`, which is a closed parser over the
    # whole tree; putting a half-check in the schema beside it would invite a
    # reader to believe the schema was the check.
    #
    # Omitted means an empty document -- somebody made a page and has not
    # written in it yet -- rather than a missing argument.
    content: JSON | None = strawberry.UNSET

    # Where the document lives. At most one; both omitted is a workspace-level
    # document, which is an ordinary state.
    #
    # Both are ids a client sends, and both are safe to accept because neither
    # is trusted: `documents_project_fk` and `documents_initiative_fk` are
    # composite through the row's own workspace, so the worst a forged one
    # achieves is a NOT_FOUND field error.
    project_id: UUID | None = None
    initiative_id: UUID | None = None

    # No `creatorId`. The author is the session's user, read off the authorized
    # scope, and is not something a client may assert about itself -- the same
    # rule `CommentCreateInput` follows, and `documents_creator_fk` is what
    # makes it true of the database rather than merely of a resolver.


@strawberry.input
class DocumentEditInput:
    workspace_slug: str
    id: UUID

    title: str | None = strawberry.UNSET
    content: JSON | None = strawberry.UNSET

    # Ask for a version boundary at this edit, whatever the heuristics say.
    #
    # A plain boolean defaulting to False rather than UNSET: "do not force one"
    # and "did not mention it" are the same request, because the heuristics run
    # either way. See `DocumentService.edit` for when a snapshot happens
    # without this.
    snapshot: bool = False

    # No `parentId` of either kind. Moving a document between a project and an
    # initiative is a distinct operation -- it changes who sees it -- and
    # folding it into the edit that also writes the body would make one
    # mutation whose access consequences depend on which fields happen to be
    # present. It is not implemented yet; when it is, it is its own mutation.


@strawberry.input
class DocumentDeleteInput:
    workspace_slug: str
    id: UUID


@strawberry.input
class DocumentRestoreInput:
    """Put a past version of a document back.

    Both ids are required and both are checked: the revision must belong to
    THIS document as well as to this workspace, so a revision id borrowed from
    another document cannot be written into this one. See
    `DocumentRepository.restore`.
    """

    workspace_slug: str
    document_id: UUID
    revision_id: UUID


@strawberry.input
class DocumentCommentCreateInput:
    workspace_slug: str
    document_id: UUID
    body: str

    # No `authorId`, for `CommentCreateInput`'s reason.


@strawberry.input
class DocumentCommentDeleteInput:
    workspace_slug: str
    id: UUID
