"""Documents, their version history, their discussion -- and the content parser.

The entities are the shape migrations/023_documents.sql stores. The interesting
half of this module is everything below `-- the parser`.

A document's content is a ProseMirror/TipTap tree stored as JSONB, and those
bytes are chosen by whoever wrote the document. That makes them client input
which has been sitting in a table long enough to look like server state -- the
same trap `app.domain.saved_views.decode_filter` exists for, with a sharper
edge, because a stored filter ends up as SQL parameters this server binds while
a stored document ends up as DOM in somebody else's browser.

So `parse_content` parses rather than trusts, in both directions:

  * on the way in, from the GraphQL mutation, so a crafted tree is refused as
    ordinary bad input before it is stored;
  * on the way OUT, in `DocumentRepository`, so a row written by a hand-run
    UPDATE, a bulk import, a restored backup or a future second writer is
    refused at the repository instead of being rendered by a client.

The parse is CLOSED: an unknown node type, an unknown mark, an unknown
attribute key and an out-of-vocabulary value are all refused rather than
dropped. Dropping is the tempting alternative and it is worse -- a document
that says something this server does not understand would silently become a
document that says LESS, which for a spec is a paragraph quietly deleted, and
for a link mark is an href silently unlinked.

What comes out is a tree this module BUILT, key by key, from values it parsed.
No part of the caller's object survives into it by reference, so there is
nothing left for a later reader to have to remember to be careful about.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID


# ------------------------------------------------------------------- bounds

# The title bound `documents_title_length` enforces, restated so a client gets
# a field error naming the number rather than a CheckViolationError carrying a
# rendered constraint.
TITLE_MIN_LENGTH: Final = 1
TITLE_MAX_LENGTH: Final = 200

# The body bound `document_comments_body_length` enforces. 007's number, for
# 007's reasons.
COMMENT_BODY_MIN_LENGTH: Final = 1
COMMENT_BODY_MAX_LENGTH: Final = 16384

# How large a document may be, measured as characters of the canonical JSON
# this module renders.
#
# DELIBERATELY LESS THAN HALF the 524288 in `documents_content_length`, and the
# gap is arithmetic rather than slack. PostgreSQL's jsonb text output puts a
# space after every `:` and `,`; the JSON this module renders has neither. The
# two numbers therefore measure different strings, and if they were equal a
# document could pass here and fail there -- surfacing as a CheckViolationError
# on the INSERT, which is a masked internal error rather than the field error
# the author can act on. The expansion cannot exceed one character per `:` plus
# one per `,`, and a `:` here costs at least four characters (`"k":`), so the
# stored string is under 1.75x what this counts; twice is the round number
# above that, and tests/test_documents.py pins the pair.
#
# Leaving the schema's ceiling above this one makes the service's rule the one
# every author actually meets, and leaves the CHECK to do what a CHECK is for:
# catch the writer that did not come through here.
MAX_CONTENT_CHARACTERS: Final = 200_000

# How deeply nodes may nest. A list inside a quote inside a list item is three;
# twenty is far past anything a person produces by writing, and is what stops
# the recursion below being unbounded over a structure a client chooses.
MAX_CONTENT_DEPTH: Final = 20

# How many nodes one document may hold. The size bound above implies a limit
# already -- a node costs at least a dozen characters -- but stating this one
# separately is what makes the cost of a parse predictable from a constant
# rather than from arithmetic about the smallest possible node.
MAX_CONTENT_NODES: Final = 10_000

# One text run. ProseMirror splits text at every mark boundary, so a run this
# long is a paragraph with no formatting in it; the document-wide bound is the
# real limit and this only stops one pathological node.
MAX_TEXT_LENGTH: Final = 20_000

MAX_HREF_LENGTH: Final = 2048
MAX_LANGUAGE_LENGTH: Final = 40

# The three schemes a link may use. Not a blocklist of dangerous ones: that set
# is open-ended and grows with every browser release, while this one is closed
# and has not changed since documents had links.
ALLOWED_URL_SCHEMES: Final = frozenset({"http", "https", "mailto"})

# How long a document may sit untouched before the next edit starts a new
# version. See the revision-boundary block in migrations/023_documents.sql for
# the whole argument; the short version is that one person's continuous session
# is one version, and coming back after a break starts another.
#
# A timedelta rather than a number of seconds, so no call site has to remember
# which unit it is in.
REVISION_GAP: Final = timedelta(minutes=10)


# ------------------------------------------------------------------ errors


class DocumentContentError(Exception):
    """Content that is not a document this server will store or serve.

    `reason` is one of the module-level constants below and NEVER contains any
    part of the offending document. That is not squeamishness: the message
    travels outward into a GraphQL payload, and a message quoting the input
    would echo attacker-chosen text back into the client that sent it -- and
    into every log between here and there.

    Raised by `parse_content` in both directions. What the two directions MEAN
    differs completely, so the caller decides: `DocumentService` turns it into
    a field error on the way in, because the client can fix its request, and
    `DocumentRepository` turns it into `InvalidStoredContentError` on the way
    out, because a stored row the parser refuses is a defect and reporting it
    as bad input would tell a reader to correct a request that was never the
    problem.
    """

    def __init__(self, reason: str):
        super().__init__(reason)

        self.reason = reason


class InvalidStoredContentError(Exception):
    """A stored document was not one this application wrote.

    Deliberately NOT a ValidationError, for the reason
    `InvalidStoredFilterError` gives: every document reaching the table went
    through `parse_content`, so a row that fails to parse on the way out is not
    input a client can correct.

    Softening it -- returning the document with the offending node stripped --
    is the tempting alternative and is the worst of the three: it would serve a
    document that says less than the row holds, from a code path that has just
    established the row is untrustworthy.

    Carries no detail about which node failed. Whoever catches this holds the
    row and can log it inside the trust boundary; the document is user text and
    does not belong in a message that travels outward.
    """

    def __init__(self):
        super().__init__("Stored document content is not readable")


# The refusals, as fixed strings. Constants rather than literals at each raise
# so that the vocabulary is enumerable -- these are a public contract the
# moment one of them reaches a client, and a message invented inline is one
# nobody reviewed.
NOT_A_DOCUMENT: Final = "Content must be a ProseMirror document object"
UNKNOWN_NODE: Final = "Content contains a node type this server does not accept"
UNKNOWN_MARK: Final = "Content contains a formatting mark this server does not accept"
UNKNOWN_ATTRIBUTE: Final = "Content contains an attribute this server does not accept"
MISSING_ATTRIBUTE: Final = "Content is missing an attribute a node it uses requires"
BAD_ATTRIBUTE: Final = "Content contains an attribute value this server does not accept"
BAD_TEXT: Final = "Content contains a malformed text run"
BAD_LINK: Final = "A link address must be an absolute http, https or mailto URL"
TOO_DEEP: Final = f"Content may be nested at most {MAX_CONTENT_DEPTH} levels deep"
TOO_MANY_NODES: Final = f"Content may hold at most {MAX_CONTENT_NODES} nodes"
TOO_LARGE: Final = f"Content may be at most {MAX_CONTENT_CHARACTERS} characters"


# ---------------------------------------------------------------- entities


@dataclass(frozen=True, slots=True)
class DocumentContent:
    """One parsed document tree.

    `root` is the mapping THIS MODULE built, never the object handed to
    `parse_content`. Holding the caller's object would make the type a label
    on unvalidated data rather than evidence about it, which is exactly the
    confusion the whole module exists to prevent -- and the parse would then be
    something a reader has to check happened, instead of something the type
    says did.

    Frozen, which freezes the reference and not the dict behind it; Python has
    no immutable mapping in the standard library that JSON-serialises without a
    conversion. Nothing mutates it, and the parse being the only constructor is
    what the guarantee actually rests on.
    """

    root: dict[str, object]


@dataclass(frozen=True, slots=True)
class DocumentEntity:
    """One document's identity and metadata -- deliberately without its body.

    The content is NOT here, and that absence is a decision rather than an
    oversight. A page of fifty documents carrying up to 200,000 characters each
    is ten megabytes of response for a screen that renders a list of titles, so
    the content is fetched per document through
    `DocumentService.contents_for_documents` and batched by a loader -- which
    means a query that does not select it never reads it, and one that selects
    it across a page pays one round trip rather than fifty.

    No `workspace_id`, matching every other entity here: the workspace is how
    an operation is scoped, not something an entity carries around afterwards.
    A caller that could read it off an entity would eventually pass it back
    down as the scope for the next call, which is how a tenant boundary stops
    being an argument the caller has to supply and becomes one it can launder.

    `project_id` and `initiative_id` are never both set -- `documents_one_parent`
    refuses that row -- so a reader may take a non-None `project_id` as meaning
    the other is None without checking. Both None is a workspace-level
    document, which is an ordinary state.

    `last_edited_by` is carried because it is half of the revision boundary
    rule, not merely because it renders: an editor who is not this person
    forces a snapshot. See `DocumentService.edit`.
    """

    id: UUID
    title: str
    project_id: UUID | None
    initiative_id: UUID | None
    creator_id: UUID
    last_edited_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentRevisionEntity:
    """One superseded version of one document, without its body.

    Without its body for `DocumentEntity`'s reason, and more so: a history is
    read as a list far more often than any one version of it is opened.

    `author_id` is who WROTE this version, not who replaced it. The snapshot is
    taken by the next editor, so recording them would attribute every version
    to the person who superseded it -- the exact opposite of what a history is
    for.

    `created_at` is when this version stopped being current. No `updated_at`,
    matching the table: a revision is what the document said at a moment and
    there is no edit path, so a second timestamp could only ever equal the
    first.
    """

    id: UUID
    document_id: UUID
    title: str
    author_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentCommentEntity:
    """One comment on one document.

    `CommentEntity` with `issue_id` swapped for `document_id`, and every
    decision in that class's docstring applies unchanged -- including why the
    workspace is absent and why `edited_at` is a different claim from
    `updated_at`.
    """

    id: UUID
    document_id: UUID
    author_id: UUID
    body: str
    edited_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentFilter:
    """Which documents a list is asking for, beyond the workspace.

    `None` is a filter and not an absence, exactly as it is on `IssueFilter`:
    `project_id=None` selects the documents belonging to no project, which is
    how "the workspace's own documents" is asked for. A field left off entirely
    is a `None` on this dataclass' OTHER meaning, which is why there are two
    fields rather than one `parent` -- see `DocumentRepository.list` for how
    the two combine.

    Deliberately not tri-state with an UNSET sentinel, unlike `IssueFilter`.
    Only two filters exist here and they are mutually exclusive by the same
    CHECK that makes the columns mutually exclusive, so "filter on project" and
    "filter on no project" are the same clause with a different bound value,
    and the third case -- not filtering -- is the whole dataclass being at its
    default.
    """

    project_id: UUID | None = None
    initiative_id: UUID | None = None
    # Whether `project_id` / `initiative_id` above are a filter at all. Two
    # booleans rather than an UNSET sentinel because a filter is either applied
    # or not, and `False` reads at the call site as the thing it means.
    by_project: bool = False
    by_initiative: bool = False


@dataclass(frozen=True, slots=True)
class DocumentPage:
    """A forward keyset page of documents, newest first.

    The same three fields as IssuePage and for the same reasons: the nodes, a
    flag the caller cannot compute for itself, and the position to resume from.
    """

    nodes: list[DocumentEntity]
    has_next_page: bool
    end_cursor: str | None


@dataclass(frozen=True, slots=True)
class DocumentRevisionPage:
    nodes: list[DocumentRevisionEntity]
    has_next_page: bool
    end_cursor: str | None


@dataclass(frozen=True, slots=True)
class DocumentCommentPage:
    nodes: list[DocumentCommentEntity]
    has_next_page: bool
    end_cursor: str | None


# ------------------------------------------------------------------ the parser


def _parse_heading_level(raw: object) -> int:
    """A heading level, 1 to 6, and specifically not a bool.

    `isinstance(True, int)` is True in Python, so a plain int check would let
    `{"level": true}` through as a level-1 heading -- a document nobody wrote.
    `_decode_priority` in app/domain/saved_views.py refuses the same trick for
    the same reason.
    """
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 6:
        raise DocumentContentError(BAD_ATTRIBUTE)

    return raw


def _parse_list_start(raw: object) -> int:
    """Where an ordered list starts counting."""
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 10_000:
        raise DocumentContentError(BAD_ATTRIBUTE)

    return raw


def _parse_checked(raw: object) -> bool:
    """A checkbox, which must be an actual JSON boolean.

    Not truthiness. `{"checked": 1}` and `{"checked": "yes"}` are documents
    something other than a TipTap client produced, and accepting them would
    make this the one attribute whose type comes from the shape of the value
    rather than from the key.
    """
    if not isinstance(raw, bool):
        raise DocumentContentError(BAD_ATTRIBUTE)

    return raw


def _parse_language(raw: object) -> str | None:
    """A code block's language, or None for an unlabelled block.

    The character class is the point rather than the length. Renderers put this
    value into `class="language-<x>"`, so a language of `x" onmouseover="...`
    is an attribute-injection payload that arrives as a perfectly ordinary
    JSON string -- and it would be one this server had stored, which is what
    makes it dangerous rather than merely rude. Restricting it to the shape a
    language identifier actually has removes the whole category.

    `str.isascii()` before the loop keeps the check honest about what it is
    testing: a Unicode character that happens to be alphanumeric is not a
    language name any highlighter knows.
    """
    if raw is None:
        return None

    if not isinstance(raw, str) or not 1 <= len(raw) <= MAX_LANGUAGE_LENGTH:
        raise DocumentContentError(BAD_ATTRIBUTE)

    if not raw.isascii() or not all(
        character.isalnum() or character in "+-._#" for character in raw
    ):
        raise DocumentContentError(BAD_ATTRIBUTE)

    return raw


def _parse_href(raw: object) -> str:
    """A link address, or refuse the document.

    This is the single most important function in the module. A stored
    `javascript:alert(document.cookie)` in a link mark is a cross-site
    scripting payload that every reader of the document executes by clicking,
    delivered through a field the schema regards as an ordinary string. An
    allowlist of SCHEMES is the only check that closes it, because the set of
    dangerous schemes is open-ended -- `javascript:`, `data:`, `vbscript:`,
    and whatever a browser adds next -- while the set of useful ones is three.

    The control-character refusal comes FIRST and is not decoration.
    `urlsplit` follows the WHATWG rule of stripping ASCII tabs and newlines
    before parsing, so `java\\nscript:alert(1)` parses with the scheme
    `javascript` -- which this function would correctly refuse -- but a variant
    that parsed as something harmless while the STORED string still held the
    control characters would be handed to a browser that strips them too, and
    the browser would see the scheme this function did not. Refusing the
    characters outright means the string this server checked and the string a
    client receives cannot be read differently by anyone.

    Surrounding whitespace is refused rather than trimmed, for the reason
    `IssueService` gives about titles: the value that comes back must be the
    value that went in, and a parser that quietly rewrites its input is one
    whose output nobody can predict from its rules.

    Relative addresses are refused too. `/handbook` would be safe and is not
    accepted, because "safe relative to what" is a question about the client's
    router that this server cannot answer -- and a scheme allowlist with a hole
    in it for "no scheme" is an allowlist an attacker gets to argue with.
    """
    if not isinstance(raw, str) or not 1 <= len(raw) <= MAX_HREF_LENGTH:
        raise DocumentContentError(BAD_LINK)

    if raw != raw.strip():
        raise DocumentContentError(BAD_LINK)

    if any(character < " " or character == "\x7f" for character in raw):
        raise DocumentContentError(BAD_LINK)

    # `urlsplit` lowercases the scheme, so `JavaScript:` is compared as
    # `javascript` and the allowlist needs no case handling of its own.
    try:
        scheme = urlsplit(raw).scheme
    except ValueError:
        # A malformed IPv6 literal is the documented way this raises. Bad
        # input, not a defect.
        raise DocumentContentError(BAD_LINK) from None

    if scheme not in ALLOWED_URL_SCHEMES:
        raise DocumentContentError(BAD_LINK)

    return raw


# Every node type this server stores, and the attributes each may carry.
#
# The key IS the vocabulary: a type absent from this mapping is refused, and an
# attribute key absent from a type's own mapping is refused for that type. So
# adding a node means adding a line here and nothing else, and forgetting to
# add one is a refusal rather than an unchecked passthrough -- which is the
# direction a closed parse has to fail in.
_NODE_ATTRIBUTES: Final[dict[str, dict[str, Callable[[object], object]]]] = {
    "paragraph": {},
    "text": {},
    "heading": {"level": _parse_heading_level},
    "bulletList": {},
    "orderedList": {"start": _parse_list_start},
    "listItem": {},
    "taskList": {},
    "taskItem": {"checked": _parse_checked},
    "blockquote": {},
    "codeBlock": {"language": _parse_language},
    "horizontalRule": {},
    "hardBreak": {},
}

# Nodes that hold no children. A `content` key on one of these is refused
# rather than ignored: a paragraph nested inside a hard break is not something
# a renderer knows what to do with, so storing it would put a decision on every
# future client instead of here.
_LEAF_NODES: Final = frozenset({"text", "horizontalRule", "hardBreak"})

# Attributes without which a node is meaningless. Only one today, and it is
# spelled as a table rather than as an `if` so that the second one is a line
# rather than a branch.
_REQUIRED_NODE_ATTRIBUTES: Final[dict[str, tuple[str, ...]]] = {
    "heading": ("level",),
}

# Every formatting mark, and the attributes each may carry. The same closed
# vocabulary as the nodes, and `link` is the only one that carries anything --
# which is why `_parse_href` above is where the security of this module
# concentrates.
_MARK_ATTRIBUTES: Final[dict[str, dict[str, Callable[[object], object]]]] = {
    "bold": {},
    "italic": {},
    "strike": {},
    "code": {},
    "link": {"href": _parse_href},
}

_REQUIRED_MARK_ATTRIBUTES: Final[dict[str, tuple[str, ...]]] = {
    "link": ("href",),
}

# The keys a node object may hold. ProseMirror's own serialisation uses exactly
# these; anything else is a client sending something this server has no
# definition for.
_NODE_KEYS: Final = frozenset({"type", "attrs", "content", "marks", "text"})
_MARK_KEYS: Final = frozenset({"type", "attrs"})


class _Budget:
    """How many nodes are left to parse.

    A tiny mutable object rather than a returned count, because the recursion
    below is depth-first over a tree and threading a running total back up
    through every call would make the bound something each branch has to
    remember to propagate. One object, decremented once per node, is a bound
    that cannot be forgotten in a branch nobody reads.
    """

    __slots__ = ("remaining",)

    def __init__(self, remaining: int):
        self.remaining = remaining

    def spend(self) -> None:
        self.remaining -= 1

        if self.remaining < 0:
            raise DocumentContentError(TOO_MANY_NODES)


def parse_content(payload: object) -> DocumentContent:
    """A candidate document tree, parsed into one this server will serve.

    Raises `DocumentContentError` and nothing else on bad input. What the
    caller does with that depends on which direction the document was moving;
    see that exception.

    The size check runs LAST, over the tree this function built rather than
    over the caller's object, and the difference matters: a client could
    otherwise send a small document padded with megabytes of whitespace, or a
    large one printed compactly, and the number checked would not be the number
    stored. Rendering the canonical form first makes the bound a fact about
    what goes into the column.
    """
    if not isinstance(payload, dict):
        raise DocumentContentError(NOT_A_DOCUMENT)

    if not payload.keys() <= {"type", "content"}:
        raise DocumentContentError(NOT_A_DOCUMENT)

    if payload.get("type") != "doc":
        raise DocumentContentError(NOT_A_DOCUMENT)

    budget = _Budget(MAX_CONTENT_NODES)
    root: dict[str, object] = {
        "type": "doc",
        # An absent `content` is an empty document rather than a malformed one:
        # that is what a client sends for a document nobody has written in yet,
        # and refusing it would make "create an empty document" impossible.
        "content": _parse_nodes(payload.get("content", []), depth=1, budget=budget),
    }

    if len(render_content(DocumentContent(root=root))) > MAX_CONTENT_CHARACTERS:
        raise DocumentContentError(TOO_LARGE)

    return DocumentContent(root=root)


def render_content(content: DocumentContent) -> str:
    """The canonical JSON text for a parsed tree.

    One renderer, used by the repository to bind the parameter AND by
    `parse_content` to measure the size, so the string that was measured is the
    string that is stored. Fixed separators and sorted keys make it
    deterministic, for the reason `app.domain.pagination._encode_payload` gives
    about cursors: the same tree always renders to the same bytes, so two
    documents that are equal are byte-equal.

    `ensure_ascii=False` keeps text as text rather than expanding every
    non-ASCII character into a six-character escape -- which would make the
    size bound mean something different for a document written in Japanese
    than for the same document written in English.
    """
    return json.dumps(
        content.root,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )


def _parse_nodes(raw: object, *, depth: int, budget: _Budget) -> list[object]:
    """One `content` array."""
    if not isinstance(raw, list):
        raise DocumentContentError(NOT_A_DOCUMENT)

    return [_parse_node(item, depth=depth, budget=budget) for item in raw]


def _parse_node(raw: object, *, depth: int, budget: _Budget) -> dict[str, object]:
    """One node, rebuilt from values this function parsed.

    The depth check is before the recursion rather than after it, so a tree
    engineered to be deep is refused at the limit rather than after the
    interpreter has already been that many frames down.
    """
    if depth > MAX_CONTENT_DEPTH:
        raise DocumentContentError(TOO_DEEP)

    budget.spend()

    if not isinstance(raw, dict) or not raw.keys() <= _NODE_KEYS:
        raise DocumentContentError(UNKNOWN_ATTRIBUTE)

    node_type = raw.get("type")

    if not isinstance(node_type, str) or node_type not in _NODE_ATTRIBUTES:
        raise DocumentContentError(UNKNOWN_NODE)

    node: dict[str, object] = {"type": node_type}

    attributes = _parse_attributes(
        raw.get("attrs"),
        allowed=_NODE_ATTRIBUTES[node_type],
        required=_REQUIRED_NODE_ATTRIBUTES.get(node_type, ()),
        unknown=UNKNOWN_ATTRIBUTE,
    )

    # Emitted only when there is something to say, so a paragraph is
    # `{"type":"paragraph","content":[...]}` and not that plus an empty attrs
    # object. Fewer bytes in the column, and -- because `render_content` sorts
    # keys -- one canonical spelling per node rather than two that compare
    # unequal.
    if attributes:
        node["attrs"] = attributes

    if node_type == "text":
        node["text"] = _parse_text(raw.get("text"))
    elif "text" in raw:
        # A `text` key on a non-text node is a document ProseMirror cannot
        # produce, and a renderer that ignored it while a search indexer read
        # it would be two clients disagreeing about what the document says.
        raise DocumentContentError(BAD_TEXT)

    if "marks" in raw:
        if node_type != "text":
            # Marks are inline formatting, and formatting a bullet list is not
            # a thing ProseMirror expresses. Allowing it would store a mark no
            # renderer applies -- which is a mark a future renderer might.
            raise DocumentContentError(UNKNOWN_MARK)

        marks = _parse_marks(raw["marks"])

        if marks:
            node["marks"] = marks

    if "content" in raw:
        if node_type in _LEAF_NODES:
            raise DocumentContentError(UNKNOWN_NODE)

        node["content"] = _parse_nodes(
            raw["content"],
            depth=depth + 1,
            budget=budget,
        )

    return node


def _parse_text(raw: object) -> str:
    """One text run.

    Empty text is refused because ProseMirror cannot produce it -- an empty
    text node is an invalid document in its own schema -- and because a
    renderer walking a tree full of them is doing work for nothing.

    NUL is refused separately from the other control characters, which are
    allowed: a tab or a newline inside a code block is ordinary content, while
    PostgreSQL cannot store `\\u0000` in jsonb at all. Refusing it here turns a
    driver-level DataError into a field error the author can act on.
    """
    if not isinstance(raw, str) or not 1 <= len(raw) <= MAX_TEXT_LENGTH:
        raise DocumentContentError(BAD_TEXT)

    if "\x00" in raw:
        raise DocumentContentError(BAD_TEXT)

    return raw


def _parse_marks(raw: object) -> list[object]:
    if not isinstance(raw, list):
        raise DocumentContentError(UNKNOWN_MARK)

    return [_parse_mark(item) for item in raw]


def _parse_mark(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or not raw.keys() <= _MARK_KEYS:
        raise DocumentContentError(UNKNOWN_MARK)

    mark_type = raw.get("type")

    if not isinstance(mark_type, str) or mark_type not in _MARK_ATTRIBUTES:
        raise DocumentContentError(UNKNOWN_MARK)

    mark: dict[str, object] = {"type": mark_type}

    attributes = _parse_attributes(
        raw.get("attrs"),
        allowed=_MARK_ATTRIBUTES[mark_type],
        required=_REQUIRED_MARK_ATTRIBUTES.get(mark_type, ()),
        unknown=UNKNOWN_MARK,
    )

    if attributes:
        mark["attrs"] = attributes

    return mark


def _parse_attributes(
    raw: object,
    *,
    allowed: dict[str, Callable[[object], object]],
    required: tuple[str, ...],
    unknown: str,
) -> dict[str, object]:
    """The `attrs` object of one node or mark, parsed key by key.

    The parser for a value comes from the KEY, never from the shape of the
    value, so a document cannot smuggle a string into the heading-level
    comparison or a number into an href. That is the same rule
    `app.domain.pagination._parse_order_key` follows and for the same reason:
    a type chosen by the input is not a type check.

    A `null` value is refused for every key EXCEPT where the key's own parser
    accepts one -- `_parse_language` does, because an unlabelled code block is
    a real thing. So the nullability decision lives with the field it applies
    to rather than in a flag here that a caller has to get right.
    """
    if raw is None:
        attributes: dict[str, object] = {}
    elif isinstance(raw, dict):
        if not raw.keys() <= allowed.keys():
            raise DocumentContentError(unknown)

        attributes = {key: allowed[key](value) for key, value in raw.items()}
    else:
        raise DocumentContentError(unknown)

    for key in required:
        if key not in attributes:
            raise DocumentContentError(MISSING_ATTRIBUTE)

    return attributes


# The empty document. What `documentCreate` stores when the client sends no
# content -- somebody made a document and has not written in it yet, which is a
# state and not an omission. A function rather than a constant because the tree
# behind a DocumentContent is a mutable dict, and one shared instance would be
# one object every empty document in the process aliased.
def empty_content() -> DocumentContent:
    return DocumentContent(root={"type": "doc", "content": []})
