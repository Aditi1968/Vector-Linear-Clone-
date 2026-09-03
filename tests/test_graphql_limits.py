"""Operation limits: what a document is refused for. No database involved.

Every limit here has to hold at *validation* time. A document that gets as
far as a resolver has already cost the server a connection and a query, so
"rejected" is only worth asserting alongside "and the service was never
called" -- which is what `RecordingIssueService` is for.

The limits are written out as literals rather than imported from
`app.graphql.limits`. A test that builds its over-limit query out of the
constant it is checking passes whatever that constant becomes, which is
exactly the regression these tests exist to catch;
`test_configured_limits_match_this_module` is what ties the two together.

Fragments get their own tests throughout. A named, nested or inline
fragment is the obvious way to try to hide selections from a counter that
walks the operation, so each budget is asserted twice: once against the
plain document and once against the same document folded into fragments.
"""

import time

import pytest
import strawberry
from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    get_introspection_query,
    get_named_type,
    parse,
)

from app.config import Environment
from app.domain.pagination import IssuePage
from app.graphql import limits
from app.graphql.limits import operation_limit_extensions
from app.graphql.schema import build_schema

from tests.conftest import make_entity


ALL_ENVIRONMENTS: list[Environment] = ["development", "test", "production"]

DEPTH_LIMIT = 10
ALIAS_LIMIT = 15
SELECTION_LIMIT = 2000
COMPLEXITY_LIMIT = 1000

# Levels of a doubling fragment chain: 2**20 selections from 839 bytes.
# A memoised walk answers in twenty multiplications; the walk this replaced
# enumerated all 1,048,576 of them and took about sixteen seconds.
FRAGMENT_BOMB_LEVELS = 20

# Every scalar an Issue has, plus the connection's own page info: the
# widest selection a client can legitimately make of one page.
FULL_PAGE_SELECTION = """
    nodes {
      id
      title
      description
      priority
      completedAt
      createdAt
      updatedAt
    }

    pageInfo {
      hasNextPage
      endCursor
    }
"""


class Context:
    def __init__(self, issue_service):
        self.issue_service = issue_service


class RecordingIssueService:
    """Counts the calls a rejected document must not have made."""

    def __init__(self, page: IssuePage):
        self._page = page
        self.calls = 0

    async def list(self, *, first: int, after: str | None):
        self.calls += 1

        return self._page


@strawberry.type
class Chain:
    """A type that contains itself, which the product schema has none of.

    `issues -> nodes -> scalars` bottoms out two levels down, so no query
    the real schema accepts can reach a depth limit of 10 at all. The rule
    is therefore exercised against a schema that can nest arbitrarily,
    using the product's own extension lineup.
    """

    name: str

    @strawberry.field
    def next(self) -> "Chain":
        return Chain(name="link")


@strawberry.type
class ChainQuery:
    @strawberry.field
    def chain(self) -> Chain:
        return Chain(name="root")


chain_schema = strawberry.Schema(
    query=ChainQuery,
    extensions=operation_limit_extensions(),
)


def chain_query(depth: int) -> str:
    """A query nesting `depth` fields that have sub-selections."""
    body = "name"

    for _ in range(depth - 1):
        body = f"next {{ {body} }}"

    return f"query Chain {{ chain {{ {body} }} }}"


def fragment_chain_query(depth: int) -> str:
    """The same nesting, one named fragment per level.

    Each fragment holds a single level and spreads the next, so the whole
    document reads as `depth` shallow definitions unless the counter
    follows the spreads.
    """
    definitions = ["query Chain { chain { ...Level1 } }"]

    for level in range(1, depth):
        inner = "name" if level == depth - 1 else f"...Level{level + 1}"
        definitions.append(f"fragment Level{level} on Chain {{ next {{ {inner} }} }}")

    return "\n".join(definitions)


def issues_query(*selections: str) -> str:
    """A query over the product schema's one list field."""
    return "query Issues {\n" + "\n".join(selections) + "\n}"


def messages(result) -> list[str]:
    return [error.message for error in result.errors or []]


def rejected_for(result, reason: str) -> bool:
    return any(reason in message for message in messages(result))


def context(page: IssuePage | None = None) -> Context:
    if page is None:
        page = IssuePage(nodes=[make_entity(1)], has_next_page=False, end_cursor=None)

    return Context(RecordingIssueService(page))


def measure_naively(selections, parent_type, schema, fragments, expanding):
    """Reference walk: the same rules, expanded in full every time.

    This is the shape `_measure` had before it memoised anything -- every
    spread re-walked at every site -- which is why it is only ever run on
    small documents here. It shares the rule's leaf-level helpers (page
    size, field and type resolution) on purpose: the question these tests
    ask is whether memoising the *walk* changed any number, so everything
    below the walk is deliberately the same code rather than a second guess
    at it.
    """
    depth = 0
    selection_count = 0
    complexity = 0
    aliases = 0

    for node in selections:
        if isinstance(node, FieldNode):
            selection_count += 1

            if node.alias is not None:
                aliases += 1

            if node.name.value.startswith("__"):
                continue

            if node.selection_set is None:
                complexity += 1
                continue

            field_def = limits._field_definition(parent_type, node.name.value)
            inner = measure_naively(
                node.selection_set.selections,
                get_named_type(field_def.type) if field_def is not None else None,
                schema,
                fragments,
                expanding,
            )

            depth = max(depth, 1 + inner.depth)
            selection_count += inner.selections
            complexity += limits._page_size(node, field_def) * inner.complexity
            aliases += inner.aliases
            continue

        if isinstance(node, InlineFragmentNode):
            inner = measure_naively(
                node.selection_set.selections,
                limits._condition_type(schema, node.type_condition, parent_type),
                schema,
                fragments,
                expanding,
            )
        elif isinstance(node, FragmentSpreadNode):
            name = node.name.value
            fragment = fragments.get(name)

            if fragment is None or name in expanding:
                continue

            inner = measure_naively(
                fragment.selection_set.selections,
                limits._condition_type(schema, fragment.type_condition, None),
                schema,
                fragments,
                expanding | {name},
            )
        else:
            continue

        depth = max(depth, inner.depth)
        selection_count += inner.selections
        complexity += inner.complexity
        aliases += inner.aliases

    return limits._Measurement(
        depth=depth,
        selections=selection_count,
        complexity=complexity,
        aliases=aliases,
    )


def measure_both(document: str):
    """(memoised, naive) measurements of every operation in a document."""
    schema = build_schema("test")._schema
    parsed = parse(document)
    fragments = {
        definition.name.value: definition
        for definition in parsed.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }

    pairs = []

    for definition in parsed.definitions:
        if not isinstance(definition, OperationDefinitionNode):
            continue

        root = limits._root_type(schema, definition)
        pairs.append(
            (
                limits._measure(
                    definition.selection_set.selections,
                    root,
                    limits._Walk(schema=schema, fragments=fragments, measured={}),
                    frozenset(),
                ),
                measure_naively(
                    definition.selection_set.selections,
                    root,
                    schema,
                    fragments,
                    frozenset(),
                ),
            )
        )

    return pairs


def diamond(levels: int) -> str:
    """A document expanding to 2**levels selections; each fragment used twice."""
    definitions = ["query Bomb { issues(first: 1) { nodes { ...F0 } } }"]

    for level in range(levels):
        spreads = " ".join([f"...F{level + 1}"] * 2)
        definitions.append(f"fragment F{level} on Issue {{ {spreads} }}")

    definitions.append(f"fragment F{levels} on Issue {{ id }}")

    return "\n".join(definitions)


EQUIVALENT_DOCUMENTS = {
    "plain": "query Q { issues(first: 5) { nodes { id title } } }",
    "defaulted page": f"query Q {{ issues {{ {FULL_PAGE_SELECTION} }} }}",
    "variable page": (
        f"query Q($n: Int!) {{ issues(first: $n) {{ {FULL_PAGE_SELECTION} }} }}"
    ),
    "aliases": (
        "query Q { a: issues { nodes { id } } b: issues { nodes { id title } } }"
    ),
    "named fragment": (
        "query Q { issues { ...P } }\n"
        "fragment P on IssueConnection { nodes { id title } "
        "pageInfo { hasNextPage } }"
    ),
    "nested fragments": (
        "query Q { issues { ...P } }\n"
        "fragment P on IssueConnection { nodes { ...F } pageInfo { endCursor } }\n"
        "fragment F on Issue { id title description }"
    ),
    "one fragment spread repeatedly": (
        "query Q { issues { nodes { ...F ...F ...F } } }\n"
        "fragment F on Issue { id title }"
    ),
    "spread from two places": (
        "query Q { a: issues { nodes { ...F } } b: issues { nodes { ...F } } }\n"
        "fragment F on Issue { id title priority }"
    ),
    "inline fragments": (
        "query Q { issues { ... on IssueConnection { nodes { ... { id title } } } } }"
    ),
    "inline inside named": (
        "query Q { issues { ...P } }\n"
        "fragment P on IssueConnection { ... on IssueConnection { nodes { id } } }"
    ),
    "multiple operations": (
        "query A { issues { nodes { ...F } } }\n"
        "query B { issues(first: 3) { nodes { ...F ...F } } }\n"
        "fragment F on Issue { id title }"
    ),
    "mutation": (
        'mutation M { issueCreate(input: {title: "t"}) '
        "{ issue { id title } errors { field code message } } }"
    ),
    "introspection": get_introspection_query(),
    "unknown field": "query Q { issues { nodes { titel } } }",
    "unknown fragment": "query Q { issues { nodes { ...Nope } } }",
    "unknown type condition": "query Q { issues { ... on Nope { nodes { id } } } }",
    "diamond, 4 levels": diamond(4),
    "diamond, 12 levels": diamond(12),
}


def test_configured_limits_match_this_module():
    """The literals above are the limits actually configured."""
    assert limits.MAX_DEPTH == DEPTH_LIMIT
    assert limits.MAX_ALIASES == ALIAS_LIMIT
    assert limits.MAX_SELECTIONS == SELECTION_LIMIT
    assert limits.MAX_COMPLEXITY == COMPLEXITY_LIMIT


@pytest.mark.parametrize(
    "document",
    EQUIVALENT_DOCUMENTS.values(),
    ids=list(EQUIVALENT_DOCUMENTS),
)
def test_memoising_the_walk_changed_no_number(document: str):
    """Every measurement must be what full expansion would have produced.

    Memoising a fragment is only sound because its cost does not depend on
    where it is spread, and because the three measures combine
    associatively -- depth by max, the rest by sum. If either stopped being
    true the rule would keep working and quietly undercount, which is worse
    than the slowness it replaced. So the fast walk is checked against a
    walk that expands everything, over a spread of shapes: aliases, named,
    nested, inline and repeated spreads, several operations in one
    document, and documents no other rule would accept.

    Cyclic documents are deliberately absent. A cut cycle measures whatever
    was left after the cut, which depends on where each walk entered it;
    both terminate and the document is refused for the cycle either way.
    `test_a_cyclic_fragment_is_reported_rather_than_followed` covers that.
    """
    pairs = measure_both(document)

    assert pairs

    for memoised, naive in pairs:
        assert memoised == naive


def test_the_reference_walk_really_does_expand_everything():
    """Guards the guard: an oracle that also memoised would prove nothing.

    A twelve-level diamond expands to 2**12 leaf selections plus the two
    fields above it. Seeing that number is what shows the reference walk is
    counting repeated spreads repeatedly, and that the memoised walk agrees
    with it rather than with a shortcut.
    """
    ((memoised, naive),) = measure_both(diamond(12))

    assert naive.selections == 2**12 + 2
    assert memoised.selections == naive.selections


async def test_a_query_at_the_depth_limit_is_accepted():
    result = await chain_schema.execute(chain_query(DEPTH_LIMIT))

    assert result.errors is None
    assert result.data is not None


async def test_a_query_over_the_depth_limit_is_rejected():
    result = await chain_schema.execute(chain_query(DEPTH_LIMIT + 1))

    assert rejected_for(result, "nested 11 levels deep")
    assert result.data is None


async def test_depth_hidden_in_named_fragments_is_still_rejected():
    """Nesting split across fragments is nesting all the same."""
    result = await chain_schema.execute(fragment_chain_query(DEPTH_LIMIT + 1))

    assert rejected_for(result, "nested 11 levels deep")


async def test_fragmented_depth_at_the_limit_is_still_accepted():
    """The fragment walk must not over-count either; the cap has to mean 10."""
    result = await chain_schema.execute(fragment_chain_query(DEPTH_LIMIT))

    assert result.errors is None


async def test_depth_hidden_in_inline_fragments_is_still_rejected():
    """An inline fragment selects on a type, so it adds no level of its own.

    Wrapping every level in one is therefore a way to look shallower to a
    counter that treats fragments as fields.
    """
    body = "name"

    for _ in range(DEPTH_LIMIT):
        body = f"next {{ ... on Chain {{ {body} }} }}"

    result = await chain_schema.execute(f"query Chain {{ chain {{ {body} }} }}")

    assert rejected_for(result, "nested 11 levels deep")


async def test_a_cyclic_fragment_is_reported_rather_than_followed():
    """The rule runs before graphql-core reports the cycle, so it must stop.

    Every validation rule is constructed before any of them visits, and
    this rule measures in its constructor -- NoFragmentCyclesRule has not
    run yet, so an unguarded expansion would recurse until the stack ran
    out instead of returning this error.
    """
    result = await chain_schema.execute(
        """
        query Cyclic { chain { ...A } }
        fragment A on Chain { name ...B }
        fragment B on Chain { name ...A }
        """
    )

    assert rejected_for(result, "Cannot spread fragment")


async def test_refusing_a_fragment_bomb_costs_less_than_executing_one():
    """A counter that follows spreads must not follow the same one twice.

    Each fragment here spreads the next one twice, so the document expands
    to 2**levels selections while its text stays under a kilobyte. Nothing
    is cyclic and nothing is unused, so every stock rule accepts it -- and
    a walk that re-expanded each spread on each path would spend that 2**n
    synchronously, on the event loop, to say no. Measuring each fragment
    once makes the refusal linear in the text.

    The threshold is loose on purpose: the failure it guards against is
    seconds to minutes, not milliseconds, so a slow machine cannot make
    this flaky without also being broken.
    """
    definitions = ["query Bomb { issues(first: 1) { nodes { ...F0 } } }"]

    for level in range(FRAGMENT_BOMB_LEVELS):
        spreads = " ".join([f"...F{level + 1}"] * 2)
        definitions.append(f"fragment F{level} on Issue {{ {spreads} }}")

    definitions.append(f"fragment F{FRAGMENT_BOMB_LEVELS} on Issue {{ id }}")
    document = "\n".join(definitions)

    service = RecordingIssueService(
        IssuePage(nodes=[make_entity(1)], has_next_page=False, end_cursor=None)
    )

    started = time.monotonic()
    result = await build_schema("test").execute(
        document, context_value=Context(service)
    )
    elapsed = time.monotonic() - started

    assert rejected_for(result, f"the maximum is {SELECTION_LIMIT}")
    assert service.calls == 0
    assert elapsed < 1.0, (
        f"refusing a {len(document)}-byte document took {elapsed:.2f}s"
    )


async def test_an_undefined_fragment_is_named_in_the_error():
    """A missing fragment is the client's mistake and must be reported as one.

    The rule skips spreads it cannot resolve so that KnownFragmentNamesRule
    gets to report them. Indexing them instead would raise out of
    validation, and the mask would turn that into `Internal server error`
    -- leaving a caller with a typo and nothing to fix it by.
    """
    result = await chain_schema.execute("query Missing { chain { ...Nope } }")

    assert rejected_for(result, "Unknown fragment 'Nope'")


async def test_a_query_at_the_alias_limit_is_accepted():
    aliases = " ".join(
        f"a{index}: issues(first: 1) {{ nodes {{ id }} }}"
        for index in range(ALIAS_LIMIT)
    )

    result = await build_schema("test").execute(
        issues_query(aliases),
        context_value=context(),
    )

    assert result.errors is None


async def test_a_query_over_the_alias_limit_is_rejected():
    """Aliases are how one document asks for one expensive field many times."""
    aliases = " ".join(
        f"a{index}: issues(first: 1) {{ nodes {{ id }} }}"
        for index in range(ALIAS_LIMIT + 1)
    )

    result = await build_schema("test").execute(
        issues_query(aliases),
        context_value=context(),
    )

    assert rejected_for(result, f"Query uses 16 aliases; the maximum is {ALIAS_LIMIT}.")
    assert result.data is None


async def test_aliases_hidden_in_a_fragment_are_still_rejected():
    document = (
        "query Issues { issues(first: 1) { ...Amplified } }\n"
        "fragment Amplified on IssueConnection { "
        + " ".join(f"n{index}: nodes {{ id }}" for index in range(ALIAS_LIMIT + 1))
        + " }"
    )

    result = await build_schema("test").execute(document, context_value=context())

    assert rejected_for(result, f"Query uses 16 aliases; the maximum is {ALIAS_LIMIT}.")


async def test_a_query_over_the_selection_limit_is_rejected():
    """Repeating a field is legal GraphQL, so repetition has to be counted."""
    repeated = " ".join(["id"] * (SELECTION_LIMIT + 1))

    result = await build_schema("test").execute(
        issues_query(f"issues(first: 1) {{ nodes {{ {repeated} }} }}"),
        context_value=context(),
    )

    assert rejected_for(result, f"the maximum is {SELECTION_LIMIT}")
    assert result.data is None


async def test_selections_hidden_in_repeated_fragment_spreads_are_counted():
    """One fragment spread 25 times is 25 copies of its selections.

    Counting each fragment definition once would read this document as 100
    fields; the server would still be asked for 2,500.
    """
    document = (
        "query Issues { issues(first: 1) { nodes { "
        + " ".join(["...Wide"] * 25)
        + " } } }\n"
        "fragment Wide on Issue { " + " ".join(["id"] * 100) + " }"
    )

    result = await build_schema("test").execute(document, context_value=context())

    assert rejected_for(result, f"the maximum is {SELECTION_LIMIT}")


async def test_a_query_over_the_complexity_budget_is_rejected():
    """Two full pages cost twice one, even though the document barely grew.

    Neither the alias cap nor the selection cap sees anything wrong here --
    two aliases and twenty-four fields -- which is the point of weighting
    a list field by the cardinality it asked for.
    """
    page = f"issues(first: 100) {{ {FULL_PAGE_SELECTION} }}"

    result = await build_schema("test").execute(
        issues_query(f"a: {page}", f"b: {page}"),
        context_value=context(),
    )

    assert rejected_for(result, f"the maximum is {COMPLEXITY_LIMIT}")
    assert result.data is None


async def test_complexity_hidden_in_fragments_is_still_rejected():
    """The same two pages, folded into named and nested fragments."""
    document = (
        "query Issues { a: issues(first: 100) { ...Page } "
        "b: issues(first: 100) { ...Page } }\n"
        "fragment Page on IssueConnection { nodes { ...Fields } "
        "pageInfo { hasNextPage endCursor } }\n"
        "fragment Fields on Issue { id title description priority "
        "completedAt createdAt updatedAt }"
    )

    result = await build_schema("test").execute(document, context_value=context())

    assert rejected_for(result, f"the maximum is {COMPLEXITY_LIMIT}")


async def test_omitting_the_page_size_is_not_a_bypass():
    """The page size a field falls back to is the page size it is charged.

    `issues` declares `first: Int! = 50`, so a document that names no page
    size still gets 50 rows. Reading cardinality out of the document alone
    charged this 144 -- 14% of the budget -- for 16 service calls, 800 rows
    and 5,632 scalars, eight times the rows of the widest page the budget
    is supposed to permit. The page size is not the client's to withhold.
    """
    page = f"issues {{ {FULL_PAGE_SELECTION} }}"
    aliased = " ".join(f"a{index}: {page}" for index in range(ALIAS_LIMIT))
    service = RecordingIssueService(
        IssuePage(nodes=[make_entity(1)], has_next_page=False, end_cursor=None)
    )

    result = await build_schema("test").execute(
        issues_query(page, aliased),
        context_value=Context(service),
    )

    assert rejected_for(result, f"the maximum is {COMPLEXITY_LIMIT}")
    assert result.data is None
    assert service.calls == 0


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
async def test_the_widest_defaulted_page_is_accepted(environment: Environment):
    """The shape a real client sends: every field, no explicit page size.

    Charging the declared default must not price ordinary use out. This is
    50 x 9 = 450 of 1000, so a client asking for a whole page the plain way
    still fits, and so do two of them.
    """
    result = await build_schema(environment).execute(
        issues_query(f"issues {{ {FULL_PAGE_SELECTION} }}"),
        context_value=context(),
    )

    assert result.errors is None
    assert result.data is not None


async def test_two_defaulted_pages_fit_and_three_do_not():
    """Where the ceiling lands, stated once so a retune has to face it.

    This also pins that an ordinary object field is charged once: `nodes`
    and `pageInfo` declare no page-size argument, and if they inherited one
    a single page would cost 50 x 50 x 9 and nothing would ever pass.
    """
    page = f"issues {{ {FULL_PAGE_SELECTION} }}"

    accepted = await build_schema("test").execute(
        issues_query(f"a: {page}", f"b: {page}"),
        context_value=context(),
    )

    assert accepted.errors is None

    rejected = await build_schema("test").execute(
        issues_query(f"a: {page}", f"b: {page}", f"c: {page}"),
        context_value=context(),
    )

    assert rejected_for(rejected, f"the maximum is {COMPLEXITY_LIMIT}")


async def test_a_defaulted_page_inside_a_fragment_is_charged_the_same():
    """Reading the schema default needs the type the fragment selects on.

    A fragment carries its own type condition, so a paginated field only
    resolves inside one if that condition is followed. Left unresolved, the
    field has no declared default to read and the omitted page size is back
    to costing 1 -- the bypass again, one fragment deeper.
    """
    page = f"issues {{ {FULL_PAGE_SELECTION} }}"
    named = (
        "query Issues { ...Pages }\n"
        "fragment Pages on Query { "
        + " ".join(f"{letter}: {page}" for letter in "abc")
        + " }"
    )

    result = await build_schema("test").execute(named, context_value=context())

    assert rejected_for(result, f"the maximum is {COMPLEXITY_LIMIT}")

    pages = " ".join(f"{letter}: {page}" for letter in "abc")

    # An inline fragment may name a type or inherit the enclosing one, and
    # the field has to resolve either way.
    for opening in ("... on Query {", "... {"):
        result = await build_schema("test").execute(
            issues_query(f"{opening} {pages} }}"),
            context_value=context(),
        )

        assert rejected_for(result, f"the maximum is {COMPLEXITY_LIMIT}")


async def test_an_ordinary_defaulted_query_still_succeeds():
    """The smallest useful real query must stay far inside the budget."""
    result = await build_schema("test").execute(
        issues_query("issues { nodes { id title } pageInfo { hasNextPage } }"),
        context_value=context(),
    )

    assert result.errors is None
    assert result.data is not None


async def test_a_page_size_passed_as_a_variable_is_not_a_discount():
    """A variable is opaque at validation time, so it is charged in full.

    Reading `first` off the document is the only cardinality a validation
    rule can see. Charging an unreadable one as 1 would make `first: $n`
    the cheapest way to buy a large page.
    """
    page = f"issues(first: $size) {{ {FULL_PAGE_SELECTION} }}"

    result = await build_schema("test").execute(
        "query Issues($size: Int!) {" + f"a: {page} b: {page}" + "}",
        variable_values={"size": 1},
        context_value=context(),
    )

    assert rejected_for(result, f"the maximum is {COMPLEXITY_LIMIT}")


async def test_a_rejected_query_never_reaches_the_service():
    """The whole point: refusal has to happen before the work starts."""
    service = RecordingIssueService(
        IssuePage(nodes=[make_entity(1)], has_next_page=False, end_cursor=None)
    )

    page = f"issues(first: 100) {{ {FULL_PAGE_SELECTION} }}"

    rejected = await build_schema("test").execute(
        issues_query(f"a: {page}", f"b: {page}"),
        context_value=Context(service),
    )

    assert rejected.errors is not None
    assert service.calls == 0

    # And the same service really would have been called by a query that passed.
    accepted = await build_schema("test").execute(
        issues_query(f"a: {page}"),
        context_value=Context(service),
    )

    assert accepted.errors is None
    assert service.calls == 1


async def test_introspection_is_not_charged_against_the_budgets():
    """A schema browser's query is deep and wide and costs no rows.

    GraphiQL's introspection query nests roughly twelve levels; charging it
    would disable the IDE everywhere it is served.
    """
    result = await build_schema("development").execute(get_introspection_query())

    assert result.errors is None
    assert result.data is not None


async def test_production_still_refuses_introspection_under_the_limits():
    """Skipping introspection subtrees must not have skipped the guard."""
    result = await build_schema("production").execute(
        "query Introspect { __schema { queryType { name } } }"
    )

    assert result.errors is not None
    assert result.data is None


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
async def test_the_widest_legitimate_page_is_accepted(
    environment: Environment,
):
    """first: 100 is the largest page the service serves; it must fit.

    Asserted per environment because the limits are applied in all of them:
    a budget that only a production client hits is a budget nobody finds
    until it is deployed.
    """
    result = await build_schema(environment).execute(
        issues_query(f"issues(first: 100) {{ {FULL_PAGE_SELECTION} }}"),
        context_value=context(),
    )

    assert result.errors is None
    assert result.data is not None
