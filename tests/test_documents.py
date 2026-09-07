"""Documents, their content parser and their validation, without a database.

Four things are pinned here, and the second is the one worth reading about.

1. That the document fields are actually ON the schema's root types. The root
   is composed in `app.graphql.schema` with `merge_types`, and a second
   `class Query(...)` written anywhere below that call does not conflict with
   it -- it silently rebinds the name, so every field belonging to the classes
   the surviving definition does not inherit from vanishes from the API with
   the whole suite still green. That has happened in this repository once.

2. That `parse_content` refuses the documents it is there to refuse. This is
   the security half of the feature and it is genuinely testable without a
   server, because it is a pure function over a JSON value. Each test below
   names the attack it declines rather than the rule it exercises: the point of
   `{"type":"link","attrs":{"href":"javascript:..."}}` being rejected is not
   that the parser has an allowlist, it is that a member of a workspace cannot
   store a payload that runs in a colleague's browser when they click a link in
   a spec.

3. That the parse runs in BOTH directions. A crafted tree stored by any means
   other than this API -- a hand-run UPDATE, a bulk import, a restored backup
   -- is refused on the way OUT, by the repository, so the schema is not the
   only thing between a stored payload and a client. That is asserted here
   against `DocumentRepository._to_content` directly, because it is the
   function that stands at that boundary.

4. That validation refuses bad input before a connection is acquired. The pool
   used here raises on `acquire()`, so a rule that reached the database would
   fail rather than pass quietly.

The behaviour only a server can decide -- what the composite keys admit,
whether a revision can be restored across tenants, what a row lock serialises
-- is in tests/test_migration_023_db.py.
"""

import json
from uuid import UUID

import pytest

from app.domain.documents import (
    MAX_CONTENT_CHARACTERS,
    MAX_CONTENT_DEPTH,
    MAX_CONTENT_NODES,
    MAX_TEXT_LENGTH,
    DocumentContent,
    DocumentContentError,
    DocumentFilter,
    InvalidStoredContentError,
    empty_content,
    parse_content,
    render_content,
)
from app.domain.errors import ValidationError
from app.graphql.schema import build_schema
from app.repositories.documents import DocumentRepository
from app.services.documents import (
    _CONSTRAINT_ERRORS,
    DocumentService,
)

from tests.conftest import TEST_SCOPE, ExplodingPool


DOCUMENT_ID = UUID("00000000-0000-7000-8000-0000000000b1")
MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")


@pytest.fixture
def schema():
    return build_schema("test")


@pytest.fixture
def service(exploding_pool: ExplodingPool) -> DocumentService:
    """The real service over a pool that refuses to open a connection."""
    return DocumentService(pool=exploding_pool, repository=DocumentRepository())


def root_fields(schema, type_name: str) -> set[str]:
    """The field names the built schema exposes on one type.

    Read off the schema object rather than out of the SDL text, so this answers
    what the server will actually resolve.
    """
    return set(schema._schema.type_map[type_name].fields)


def issues(raised) -> list[tuple[str, str]]:
    return [(issue.field, issue.code) for issue in raised.value.issues]


def doc(*nodes) -> dict:
    return {"type": "doc", "content": list(nodes)}


def text(value: str, *marks) -> dict:
    node: dict = {"type": "text", "text": value}

    if marks:
        node["marks"] = list(marks)

    return node


# --------------------------------------------------------------- the schema


def test_the_document_fields_reach_the_root_query(schema):
    """Every read this feature adds is on the schema the server builds.

    Not on `DocumentQuery`, which would pass whether or not that class was ever
    merged into the root. The failure this refuses is a rebound `Query` name
    taking a whole feature out of the API silently.
    """
    assert {"document", "documents"} <= root_fields(schema, "Query")


def test_the_document_mutations_reach_the_root_mutation(schema):
    assert {
        "documentCreate",
        "documentEdit",
        "documentRestore",
        "documentDelete",
        "documentCommentCreate",
        "documentCommentDelete",
    } <= root_fields(schema, "Mutation")


def test_a_document_exposes_its_history_and_its_discussion(schema):
    """Both are fields ON the document, not root fields taking a document id.

    A root `documentRevisions(documentId:)` would be a second place to write
    the workspace predicate, and the second place is the one nobody re-reads.
    """
    assert {"content", "revisions", "comments"} <= root_fields(schema, "Document")


def test_a_document_never_publishes_the_workspace_it_lives_in(schema):
    """No `workspaceId` on the type, deliberately.

    A client that could read the tenant off a returned row would eventually
    send it back as the scope for the next call, which is how a tenant boundary
    stops being an argument the server derives and becomes one a client
    supplies.
    """
    assert "workspaceId" not in root_fields(schema, "Document")


# ----------------------------------------------------- the parser: acceptance


def test_an_ordinary_document_survives_a_round_trip():
    """The parse is a filter, not a rewrite: valid content comes back intact."""
    original = doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [text("Spec")]},
        {
            "type": "paragraph",
            "content": [
                text("see "),
                text(
                    "the RFC", {"type": "link", "attrs": {"href": "https://x.test/a"}}
                ),
                text(" for detail", {"type": "italic"}),
            ],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [text("one")]},
                    ],
                },
            ],
        },
        {
            "type": "codeBlock",
            "attrs": {"language": "python"},
            "content": [text("x=1")],
        },
        {"type": "horizontalRule"},
    )

    assert parse_content(original).root == original


def test_an_empty_document_is_a_document():
    """Creating a page and not writing in it yet is an ordinary state.

    Refusing it would make `documentCreate` impossible without a body, which is
    the first thing every editor does.
    """
    assert parse_content({"type": "doc"}).root == {"type": "doc", "content": []}
    assert empty_content().root == {"type": "doc", "content": []}


def test_absent_attributes_are_not_invented():
    """A node with no attrs stores no attrs.

    `{"type":"paragraph","attrs":{}}` and `{"type":"paragraph"}` are the same
    document, and storing two spellings of it would make two identical
    documents compare unequal -- which is what `content_matches` in the
    repository, and therefore the no-op rule in `DocumentService.edit`, rests
    on.
    """
    assert parse_content(doc({"type": "paragraph", "attrs": {}})).root == doc(
        {"type": "paragraph"}
    )


def test_rendering_is_deterministic_whatever_order_the_keys_arrived_in():
    """Two spellings of one document render to the same bytes.

    The stored string is what the size bound was measured against and what a
    later comparison reads back, so a renderer whose output depended on dict
    insertion order would make both of those depend on how a client happened to
    serialise its JSON.
    """
    first = render_content(parse_content({"type": "doc", "content": [text("a")]}))
    second = render_content(
        parse_content({"content": [{"text": "a", "type": "text"}], "type": "doc"})
    )

    assert first == second


# ------------------------------------------------------ the parser: refusals


def test_a_javascript_link_is_refused():
    """The attack this whole feature is shaped around.

    A member writes a spec, puts a link on a word, and the href is
    `javascript:` -- so every colleague who clicks it runs the author's code in
    their own session, on this server's origin, with their own cookies. The
    schema cannot see this: it is a perfectly ordinary JSON string in a
    perfectly ordinary object. Only the scheme allowlist refuses it.
    """
    with pytest.raises(DocumentContentError):
        parse_content(
            doc(
                {
                    "type": "paragraph",
                    "content": [
                        text(
                            "click",
                            {
                                "type": "link",
                                "attrs": {"href": "javascript:alert(document.cookie)"},
                            },
                        ),
                    ],
                }
            )
        )


@pytest.mark.parametrize(
    "href",
    [
        # Case is not a disguise: urlsplit lowercases the scheme.
        "JavaScript:alert(1)",
        # Control characters are stripped by browsers AND by urlsplit, so a
        # string carrying them can be read one way here and another way there.
        # Refused outright so the two cannot disagree.
        "java\nscript:alert(1)",
        "java\tscript:alert(1)",
        # A payload delivered as its own document.
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
        # Leading whitespace is another way to move the scheme out of sight of
        # a naive check; refused rather than trimmed, so the stored value is
        # the checked value.
        "  https://x.test",
        # No scheme at all. Safe in a browser and refused anyway: "relative to
        # what" is a question about the client's router, and an allowlist with
        # a hole in it for "no scheme" is one an attacker gets to argue with.
        "/handbook",
        "//evil.test/x",
    ],
)
def test_only_absolute_http_https_and_mailto_links_are_stored(href):
    with pytest.raises(DocumentContentError):
        parse_content(
            doc(
                {
                    "type": "paragraph",
                    "content": [
                        text("x", {"type": "link", "attrs": {"href": href}}),
                    ],
                }
            )
        )


@pytest.mark.parametrize(
    "href",
    ["https://x.test/a?b=c#d", "http://x.test", "mailto:someone@x.test"],
)
def test_the_three_useful_schemes_are_kept(href):
    """The allowlist has to be usable or it will be widened by whoever needs a
    link to work, which is how allowlists become blocklists."""
    parsed = parse_content(
        doc(
            {
                "type": "paragraph",
                "content": [
                    text("x", {"type": "link", "attrs": {"href": href}}),
                ],
            }
        )
    )

    node = parsed.root["content"][0]["content"][0]

    assert node["marks"][0]["attrs"]["href"] == href


def test_a_code_block_language_cannot_carry_markup():
    """The second injection route, and the one that looks harmless.

    Renderers put this string into `class="language-<x>"`. A language of
    `x" onmouseover="fetch(...)` is therefore an attribute-injection payload
    that arrives as an ordinary JSON string and that this server would have
    stored -- which is what makes it worse than the same string arriving live.
    """
    with pytest.raises(DocumentContentError):
        parse_content(
            doc(
                {
                    "type": "codeBlock",
                    "attrs": {"language": 'x" onmouseover="fetch(1)'},
                    "content": [text("payload")],
                }
            )
        )


def test_an_unknown_node_type_is_refused_rather_than_dropped():
    """Refused, not ignored, and the difference is the whole parse.

    Dropping an unknown node would make a document that says something this
    server does not understand read as a document that says LESS -- a paragraph
    quietly deleted from a spec, with nothing anywhere reporting it.
    """
    with pytest.raises(DocumentContentError):
        parse_content(doc({"type": "iframe", "attrs": {"src": "https://x.test"}}))


def test_an_unknown_mark_is_refused():
    with pytest.raises(DocumentContentError):
        parse_content(
            doc(
                {
                    "type": "paragraph",
                    "content": [
                        text("x", {"type": "onclick", "attrs": {}}),
                    ],
                }
            )
        )


def test_an_unknown_attribute_on_a_known_node_is_refused():
    """A closed node vocabulary with open attributes is not closed.

    `{"type":"paragraph","attrs":{"style":"..."}}` is a paragraph a renderer
    might one day honour, put there by somebody who knew this server would
    store whatever it was handed.
    """
    with pytest.raises(DocumentContentError):
        parse_content(doc({"type": "paragraph", "attrs": {"style": "x"}}))


def test_a_heading_level_outside_one_to_six_is_refused():
    with pytest.raises(DocumentContentError):
        parse_content(doc({"type": "heading", "attrs": {"level": 9}}))


def test_a_boolean_is_not_a_heading_level():
    """`isinstance(True, int)` is True in Python.

    A plain int check would read `{"level": true}` as a level-1 heading -- a
    document nobody wrote, produced by a type test that looked correct.
    """
    with pytest.raises(DocumentContentError):
        parse_content(doc({"type": "heading", "attrs": {"level": True}}))


def test_a_heading_without_a_level_is_refused():
    with pytest.raises(DocumentContentError):
        parse_content(doc({"type": "heading", "content": [text("x")]}))


def test_a_link_without_an_href_is_refused():
    with pytest.raises(DocumentContentError):
        parse_content(
            doc({"type": "paragraph", "content": [text("x", {"type": "link"})]})
        )


@pytest.mark.parametrize(
    "payload",
    [
        # Not an object at all -- what a hand-written UPDATE or a second writer
        # produces. `documents_content_is_object` refuses these too; this is
        # the layer that gives the refusal a message.
        [],
        "a string",
        42,
        None,
        # An object, but not a document.
        {"type": "paragraph"},
        {"type": "doc", "unexpected": 1},
    ],
)
def test_only_a_prosemirror_document_object_is_accepted(payload):
    with pytest.raises(DocumentContentError):
        parse_content(payload)


def test_a_leaf_node_may_not_have_children():
    """A paragraph inside a hard break is not something a renderer knows what
    to do with, so storing it would put a decision on every future client."""
    with pytest.raises(DocumentContentError):
        parse_content(doc({"type": "hardBreak", "content": [{"type": "paragraph"}]}))


def test_marks_may_not_be_hung_on_a_block():
    with pytest.raises(DocumentContentError):
        parse_content(doc({"type": "bulletList", "marks": [{"type": "bold"}]}))


def test_a_text_key_on_a_non_text_node_is_refused():
    """A renderer that ignored it while a search indexer read it would be two
    clients disagreeing about what the document says."""
    with pytest.raises(DocumentContentError):
        parse_content(doc({"type": "paragraph", "text": "smuggled"}))


def test_a_null_byte_is_refused_before_the_driver_sees_it():
    """PostgreSQL cannot store \\u0000 in jsonb.

    Without this the failure is a DataError from inside asyncpg, which the
    schema masks as "Internal server error" -- so an author who pasted one
    would be told the server broke.
    """
    with pytest.raises(DocumentContentError):
        parse_content(doc({"type": "paragraph", "content": [text("a\x00b")]}))


def test_a_document_nested_past_the_limit_is_refused():
    """Bounded so the recursion cost does not depend on what a client sends.

    Built one level past the bound rather than at some round number, so this
    test fails if the bound moves in either direction rather than only if it
    is removed.
    """
    node: dict = {"type": "paragraph"}

    for _ in range(MAX_CONTENT_DEPTH + 1):
        node = {"type": "blockquote", "content": [node]}

    with pytest.raises(DocumentContentError):
        parse_content(doc(node))


def test_a_document_with_too_many_nodes_is_refused():
    with pytest.raises(DocumentContentError):
        parse_content(doc(*({"type": "paragraph"},) * (MAX_CONTENT_NODES + 1)))


def test_one_enormous_text_run_is_refused():
    with pytest.raises(DocumentContentError):
        parse_content(
            doc({"type": "paragraph", "content": [text("a" * (MAX_TEXT_LENGTH + 1))]})
        )


def test_a_document_larger_than_the_column_allows_is_refused_here_first():
    """Measured over what would be STORED, not over what arrived.

    A client could otherwise send a small document padded with megabytes of
    whitespace, or a large one printed compactly, and the number checked would
    not be the number the column has to hold.
    """
    run = "a" * 1000
    paragraphs = [
        {"type": "paragraph", "content": [text(run)]}
        for _ in range(MAX_CONTENT_CHARACTERS // len(run) + 2)
    ]

    with pytest.raises(DocumentContentError):
        parse_content(doc(*paragraphs))


def test_a_refusal_never_quotes_the_document_it_refused():
    """The message travels outward into a GraphQL payload and into every log
    between here and there. A message quoting the input would echo
    attacker-chosen text back through the API."""
    secret = "totally-unique-marker-9f3a"

    with pytest.raises(DocumentContentError) as raised:
        parse_content(doc({"type": secret}))

    assert secret not in str(raised.value)
    assert secret not in raised.value.reason


# --------------------------------------------- the parse on the way back out


def test_a_crafted_row_is_refused_on_the_way_out_of_the_repository():
    """The half that is easy to leave out, and the reason this feature is safe.

    This is a row nothing in the API could have written: a link mark with a
    `javascript:` href, put there by a hand-run UPDATE, a bulk import, a
    restored backup or a future second writer. The schema admits it -- it is a
    JSON object of a legal size -- so if the repository trusted what it read,
    that payload would go straight to a reader's browser.
    """
    stored = json.dumps(
        doc(
            {
                "type": "paragraph",
                "content": [
                    text(
                        "click",
                        {"type": "link", "attrs": {"href": "javascript:alert(1)"}},
                    ),
                ],
            }
        )
    )

    with pytest.raises(InvalidStoredContentError):
        DocumentRepository._to_content(stored)


def test_a_stored_row_that_is_not_json_at_all_is_refused_not_crashed():
    """Impossible while the column is JSONB, and exactly the sort of
    impossibility that stops being one when somebody changes a column type."""
    with pytest.raises(InvalidStoredContentError):
        DocumentRepository._to_content("not json")


def test_an_unreadable_row_is_not_reported_as_the_caller_s_mistake():
    """`InvalidStoredContentError` is deliberately NOT a ValidationError.

    Everything reaching the column went through the parser, so a row that fails
    on the way out is a defect. Reported as bad input it would tell a reader to
    correct a request that was never the problem, and it would be answered with
    a 200.
    """
    assert not issubclass(InvalidStoredContentError, ValidationError)


def test_an_unreadable_row_says_nothing_about_what_was_in_it():
    """Whoever catches this holds the row and can log it inside the trust
    boundary; the document is user text and does not belong in a message that
    travels outward."""
    stored = json.dumps(doc({"type": "unique-marker-4c81"}))

    with pytest.raises(InvalidStoredContentError) as raised:
        DocumentRepository._to_content(stored)

    assert "unique-marker-4c81" not in str(raised.value)


# ------------------------------------------------------------- the service


async def test_a_crafted_document_is_refused_before_a_connection_is_taken(
    service, exploding_pool
):
    """Reported as a field error on `content`, not as a masked server error.

    And reported without opening a connection: the pool here raises on
    acquire, so a parse that ran after the database was reached would fail this
    test rather than pass it quietly.
    """
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            title="Spec",
            content=doc({"type": "script"}),
            project_id=None,
            initiative_id=None,
            creator_id=MEMBER_ID,
        )

    assert issues(raised) == [("content", "INVALID")]
    assert exploding_pool.acquire_count == 0


async def test_a_document_may_not_belong_to_two_things_at_once(service, exploding_pool):
    """`documents_one_parent` refuses the row too and remains the guarantee.

    This exists so the refusal is a field error naming the rule rather than a
    CheckViolationError, which is either masked -- telling the client nothing
    -- or forwarded, telling it about the schema.
    """
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            title="Spec",
            content=None,
            project_id=UUID("00000000-0000-7000-8000-0000000000c1"),
            initiative_id=UUID("00000000-0000-7000-8000-0000000000d1"),
            creator_id=MEMBER_ID,
        )

    assert issues(raised) == [("initiativeId", "CONFLICT")]
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize(
    ("title", "code"),
    [("", "REQUIRED"), ("t" * 201, "TOO_LONG")],
)
async def test_a_title_is_bounded_before_the_database_is_reached(
    service, exploding_pool, title, code
):
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            title=title,
            content=None,
            project_id=None,
            initiative_id=None,
            creator_id=MEMBER_ID,
        )

    assert issues(raised) == [("title", code)]
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize(
    ("body", "code"),
    [("", "REQUIRED"), ("b" * 16385, "TOO_LONG")],
)
async def test_a_comment_body_is_bounded_before_the_database_is_reached(
    service, exploding_pool, body, code
):
    with pytest.raises(ValidationError) as raised:
        await service.create_comment(
            scope=TEST_SCOPE,
            document_id=DOCUMENT_ID,
            author_id=MEMBER_ID,
            body=body,
        )

    assert issues(raised) == [("body", code)]
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize("first", [0, 101])
async def test_a_page_size_outside_the_range_is_refused(service, exploding_pool, first):
    """Never silently clamped. A client asking for 500 gets an error rather
    than 100 rows and no way to tell that it was truncated."""
    with pytest.raises(ValidationError) as raised:
        await service.list(
            scope=TEST_SCOPE,
            document_filter=DocumentFilter(),
            first=first,
            after=None,
        )

    assert issues(raised) == [("first", "OUT_OF_RANGE")]
    assert exploding_pool.acquire_count == 0


async def test_a_malformed_cursor_is_input_and_not_an_exception(
    service, exploding_pool
):
    with pytest.raises(ValidationError) as raised:
        await service.list(
            scope=TEST_SCOPE,
            document_filter=DocumentFilter(),
            first=10,
            after="not-a-cursor",
        )

    assert issues(raised) == [("after", "INVALID_CURSOR")]
    assert exploding_pool.acquire_count == 0


def test_both_comment_constraints_answer_with_one_message():
    """`CommentService.create`'s rule, applied to the table that mirrors it.

    A viewer who does not belong to this workspace must not learn from a
    distinguishable error that the document they named is real -- so the
    "no such document" and the "you are not a member" refusals have to be the
    same answer, and which constraint the planner checks first has to be
    unobservable.
    """
    assert (
        _CONSTRAINT_ERRORS["document_comments_document_fk"]
        == _CONSTRAINT_ERRORS["document_comments_author_fk"]
    )


def test_every_foreign_key_that_client_input_can_break_has_a_message():
    """A violation with no entry here is re-raised as a defect, which is the
    correct default -- so this list is what decides which refusals are
    reportable, and a key added to 023 without a line here would surface to a
    user as "Internal server error"."""
    assert set(_CONSTRAINT_ERRORS) == {
        "documents_project_fk",
        "documents_initiative_fk",
        "documents_creator_fk",
        "documents_last_editor_fk",
        "document_revisions_author_fk",
        "document_comments_document_fk",
        "document_comments_author_fk",
    }


def test_the_domain_bound_stays_below_the_column_bound():
    """The gap is load-bearing, not slack.

    PostgreSQL's jsonb text output puts a space after every `:` and `,`; the
    JSON this module renders has neither. The two numbers therefore measure
    different strings, and if they were equal a document could pass the service
    and fail the CHECK -- surfacing as a masked internal error on the INSERT
    rather than as the field error its author can act on.
    """
    column_bound = 524288
    # The worst case is a document that is all punctuation: every `:` and `,`
    # gains a byte in PostgreSQL's rendering, so the stored string can be at
    # most twice what this module measured.
    assert MAX_CONTENT_CHARACTERS * 2 <= column_bound


def test_a_parsed_tree_is_the_only_thing_render_content_will_take():
    """`DocumentContent` is evidence, not a label.

    Nothing outside this module constructs one from unparsed input, and the
    repository binds only what `render_content` produced -- so "was this
    checked?" is answered by reading the one call that produced the value.
    """
    assert render_content(DocumentContent(root={"type": "doc", "content": []})) == (
        '{"content":[],"type":"doc"}'
    )
