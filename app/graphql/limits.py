"""Operation limits: what a single GraphQL document is allowed to ask for.

Every limit here is enforced during *validation*, before graphql-core calls
a single resolver. That is the whole point: a query that would fan out into
thousands of database reads must be refused while it is still an AST, not
while it is being executed.

The limits are deliberately blunt. Three of them bound the shape of a
document alone and need no per-field annotation. The fourth, complexity,
also reads the schema -- but only for the page size a list field will
actually use, which is the one thing about a field's cost that a document
can lie about by omission.
"""

from collections.abc import Callable
from dataclasses import dataclass

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLField,
    GraphQLNamedType,
    GraphQLSchema,
    InlineFragmentNode,
    NamedTypeNode,
    OperationDefinitionNode,
    OperationType,
    SelectionNode,
    ValidationContext,
    ValidationRule,
    get_named_type,
)
from strawberry.extensions import AddValidationRules
from strawberry.extensions.base_extension import SchemaExtension
from strawberry.extensions.utils import is_introspection_key


# Nesting levels of composite fields. The product schema is two deep
# (issues -> nodes -> scalars), so 10 is far above anything a client needs
# while still refusing the unbounded cycles a related graph would allow.
MAX_DEPTH = 10

# Aliases let one document ask for the same expensive field many times over.
MAX_ALIASES = 15

# Total field selections, counted after fragments are expanded.
MAX_SELECTIONS = 2000

# Weighted cost: roughly the number of scalar values the server would have
# to produce. For scale, on the schema as it stands, one whole page of
# issues with every Issue field plus pageInfo costs 900 asked for
# explicitly as issues(first: 100), and 450 asked for plainly as `issues`,
# whose declared default is 50. So two plain pages in one document fit and
# three do not.
MAX_COMPLEXITY = 1000

# Cardinality charged for a page size that is knowable to neither the
# document nor the schema -- `first: $count`, whose value arrives with the
# request and not with the query. Charging the largest page any service
# will serve keeps a variable from being the cheap way to buy cardinality.
# Must stay >= the largest page size any service permits (currently
# app.services.issues.FIRST_MAX, which is 100). Imported nowhere on
# purpose: this is a transport-level guard and has no business knowing one
# field's validation rules.
ASSUMED_PAGE_SIZE = 100

# Arguments that mean "give me this many". Cursor pagination is the only
# fan-out this schema has, so these are the only two that need weighting.
PAGE_SIZE_ARGUMENTS = ("first", "last")


@dataclass(frozen=True, slots=True)
class _Measurement:
    """What one list of selections costs, in the four budgeted currencies.

    All four combine associatively -- depth by max, the rest by sum -- which
    is what lets a fragment be measured once and reused at every spread.
    """

    depth: int
    selections: int
    complexity: int
    aliases: int


_NOTHING = _Measurement(depth=0, selections=0, complexity=0, aliases=0)


@dataclass(frozen=True, slots=True)
class _Walk:
    """The parts of a walk that do not change as it descends.

    `measured` memoises each fragment's cost under its own name. A fragment
    measures the same wherever it is spread -- its selections and its type
    condition come from its definition, not from the spread site -- so the
    second spread of a fragment can read the first one's answer. Without
    that, a document whose fragments each spread the next one twice costs
    2**levels visits to reject: 772 bytes of text buying seconds of blocked
    event loop, which is a cheaper attack than the query it refuses.
    """

    schema: GraphQLSchema
    fragments: dict[str, FragmentDefinitionNode]
    measured: dict[str, _Measurement]


def _as_page_size(value: object) -> int:
    """A page size read from the document or the schema, or the assumption.

    Anything that is not a plain integer -- a variable node, an explicit
    null, graphql-core's `Undefined` for an argument with no default -- is
    a page size we do not know, and an unknown page size is charged rather
    than waved through.
    """
    try:
        # The coercion is the point, not an accident: graphql-core carries an
        # IntValueNode's value as a *string*, so `first: 50` arrives here as
        # "50" and an isinstance(int) test would send every page size the
        # document states down the ASSUMED_PAGE_SIZE path. `int(object)` has
        # no matching overload, hence the ignore; the annotation is what stops
        # that ignore from leaking Any out of a function declared to return int.
        parsed: int = int(value)  # type: ignore[call-overload]

        return max(1, parsed)
    except (TypeError, ValueError):
        return ASSUMED_PAGE_SIZE


def _page_size(node: FieldNode, field_def: GraphQLField | None) -> int:
    """Cardinality this field will actually return.

    Three sources, in the order the server itself resolves them: the value
    written in the document, then the default declared on the argument,
    then the assumption. Stopping at the first source is what made omitting
    `first` cheaper than sending it -- the server falls back to the
    declared default whether or not the document mentions it, so the budget
    has to fall back the same way.

    A field that declares no page-size argument is charged once. That is
    what keeps an ordinary object field -- `nodes`, `pageInfo` -- from
    being priced as though it fanned out.
    """
    for argument in node.arguments or ():
        if argument.name.value in PAGE_SIZE_ARGUMENTS:
            return _as_page_size(getattr(argument.value, "value", None))

    if field_def is None:
        return 1

    for name in PAGE_SIZE_ARGUMENTS:
        declared = field_def.args.get(name)

        if declared is not None:
            return _as_page_size(declared.default_value)

    return 1


def _field_definition(
    parent_type: GraphQLNamedType | None,
    name: str,
) -> GraphQLField | None:
    """The definition of `name` on `parent_type`, if there is one.

    Returns None freely: a field on a type this rule could not resolve, a
    field that does not exist, a selection on a union. Every one of those
    means some other rule is about to report the document, and none of them
    may raise from here -- the whole document would fail as an internal
    error instead of as the validation error it is.
    """
    fields: dict[str, GraphQLField] | None = getattr(parent_type, "fields", None)

    if fields is None:
        return None

    return fields.get(name)


def _condition_type(
    schema: GraphQLSchema,
    condition: NamedTypeNode | None,
    parent_type: GraphQLNamedType | None,
) -> GraphQLNamedType | None:
    """The type a fragment selects on; an inline fragment may omit it."""
    if condition is None:
        return parent_type

    return schema.get_type(condition.name.value)


def _root_type(
    schema: GraphQLSchema,
    operation: OperationDefinitionNode,
) -> GraphQLNamedType | None:
    if operation.operation == OperationType.MUTATION:
        return schema.mutation_type

    if operation.operation == OperationType.SUBSCRIPTION:
        return schema.subscription_type

    return schema.query_type


def _spread(
    name: str,
    walk: _Walk,
    expanding: frozenset[str],
) -> _Measurement:
    """What spreading the named fragment here costs.

    `expanding` holds the fragments on the current path, so a fragment that
    directly or transitively spreads itself contributes nothing rather than
    recursing forever. That guard is not redundant with graphql-core's
    NoFragmentCyclesRule: every rule is constructed before any rule visits,
    and this rule does its work in its constructor, so it runs on documents
    the cycle rule has not reported on yet.

    On an acyclic document -- every document that can execute -- the cached
    answer is the exact one, because a fragment's cost depends only on its
    own definition. On a cyclic one the numbers depend on where the walk
    entered the cycle, and are not worth straightening out: the document is
    refused for the cycle whatever they say.
    """
    if name in expanding:
        return _NOTHING

    cached = walk.measured.get(name)

    if cached is not None:
        return cached

    fragment = walk.fragments.get(name)

    # An undefined fragment is KnownFragmentNamesRule's error to report, so
    # it is skipped rather than assumed away or indexed into.
    if fragment is None:
        return _NOTHING

    measured = _measure(
        fragment.selection_set.selections,
        _condition_type(walk.schema, fragment.type_condition, None),
        walk,
        expanding | {name},
    )
    walk.measured[name] = measured

    return measured


def _measure(
    selections: tuple[SelectionNode, ...],
    parent_type: GraphQLNamedType | None,
    walk: _Walk,
    expanding: frozenset[str],
) -> _Measurement:
    """Measure one selection set, expanding fragment spreads in place.

    A fragment spread costs what the fragment costs, every time it is
    spread: a document that spreads the same fragment ten times really does
    ask the server for those fields ten times. Measuring it once and reusing
    the answer is what keeps that from being exponential.

    `parent_type` is threaded down as an argument rather than tracked with
    a graphql-core TypeInfo. TypeInfo is a mutable stack that assumes one
    linear pass over the document, and this walk visits a fragment's
    selections outside document order; a stack that desynchronised there
    would not raise, it would quietly charge the wrong cardinality. A
    parameter cannot desynchronise. Every step of the type resolution
    tolerates a type it cannot find, because on an invalid document -- an
    unknown field, an unknown fragment condition -- there will be one, and
    reporting that is another rule's job.
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

            # __schema and __type describe the schema, not the data; their
            # subtrees are deep and wide by nature and cost the database
            # nothing. Introspection is refused outright in production.
            if is_introspection_key(node.name.value):
                continue

            if node.selection_set is None:
                complexity += 1
                continue

            field_def = _field_definition(parent_type, node.name.value)
            field_type = (
                get_named_type(field_def.type) if field_def is not None else None
            )

            inner = _measure(
                node.selection_set.selections,
                field_type,
                walk,
                expanding,
            )

            depth = max(depth, 1 + inner.depth)
            selection_count += inner.selections
            complexity += _page_size(node, field_def) * inner.complexity
            aliases += inner.aliases
        else:
            # Fragments are transparent: they select on the type, not on a
            # field, so they add no nesting level and no cost of their own.
            if isinstance(node, InlineFragmentNode):
                inner = _measure(
                    node.selection_set.selections,
                    _condition_type(walk.schema, node.type_condition, parent_type),
                    walk,
                    expanding,
                )
            elif isinstance(node, FragmentSpreadNode):
                inner = _spread(node.name.value, walk, expanding)
            else:
                continue

            depth = max(depth, inner.depth)
            selection_count += inner.selections
            complexity += inner.complexity
            aliases += inner.aliases

    return _Measurement(
        depth=depth,
        selections=selection_count,
        complexity=complexity,
        aliases=aliases,
    )


class OperationLimitsRule(ValidationRule):
    """Refuse documents over the depth, alias, selection or cost budget.

    All four are measured in a single fragment-expanding walk, so a client
    cannot pay for one budget with another: moving selections into named,
    nested or inline fragments changes none of the four numbers.

    The measurement happens in the constructor rather than in visitor
    callbacks. graphql-core's `visit` walks each fragment definition once,
    which is exactly the wrong shape for a rule that has to charge repeated
    spreads repeatedly.

    Cardinality comes from the document where the document states it and
    from the schema where it does not, so a client cannot buy a cheaper
    price by leaving `first` out and letting the server fill in the default.

    Limitation: a page size supplied as a variable arrives with the request,
    not with the document, so validation cannot read it and charges
    ASSUMED_PAGE_SIZE instead. That is an over-charge for small pages, never
    an under-charge. A field that fans out without declaring a `first` or
    `last` argument would still be charged once -- this schema has none, and
    adding one means adding its argument name to PAGE_SIZE_ARGUMENTS.
    """

    def __init__(self, context: ValidationContext) -> None:
        super().__init__(context)

        document = context.document
        walk = _Walk(
            schema=context.schema,
            fragments={
                definition.name.value: definition
                for definition in document.definitions
                if isinstance(definition, FragmentDefinitionNode)
            },
            measured={},
        )

        for definition in document.definitions:
            if not isinstance(definition, OperationDefinitionNode):
                continue

            measured = _measure(
                definition.selection_set.selections,
                _root_type(context.schema, definition),
                walk,
                frozenset(),
            )

            self._report(measured)

    def _report(self, measured: _Measurement) -> None:
        """Report every budget the operation broke, not just the first.

        A client that trimmed one dimension only to be refused on the next
        would be back for another round trip, and each round trip is another
        parse and validate of the same oversized document.
        """
        if measured.depth > MAX_DEPTH:
            self.report_error(
                GraphQLError(
                    f"Query is nested {measured.depth} levels deep; "
                    f"the maximum is {MAX_DEPTH}."
                )
            )

        if measured.aliases > MAX_ALIASES:
            self.report_error(
                GraphQLError(
                    f"Query uses {measured.aliases} aliases; "
                    f"the maximum is {MAX_ALIASES}."
                )
            )

        if measured.selections > MAX_SELECTIONS:
            self.report_error(
                GraphQLError(
                    f"Query selects {measured.selections} fields; "
                    f"the maximum is {MAX_SELECTIONS}."
                )
            )

        if measured.complexity > MAX_COMPLEXITY:
            self.report_error(
                GraphQLError(
                    f"Query complexity is {measured.complexity}; "
                    f"the maximum is {MAX_COMPLEXITY}."
                )
            )


def operation_limit_extensions() -> list[Callable[[], SchemaExtension]]:
    """Extensions enforcing the limits above, for `strawberry.Schema`.

    A factory rather than an instance: strawberry 0.326.0 deprecates passing
    a built extension and constructs one per request from a callable, so
    this is the form that survives the deprecation.

    Neither of strawberry's own limiters is used, for the same reason in two
    shapes. MaxAliasesLimiter re-expands every spread on every path, so the
    772-byte document in `tests/test_phase1a1_regression.py` costs it 2**18
    visits and half a second of blocked event loop -- with no aliases in it
    at all. QueryDepthLimiter has that shape too, and additionally indexes
    `fragments[name]` for a spread it has never seen, so a document naming a
    fragment that does not exist raises KeyError out of validation instead
    of being told which fragment is missing. Counting aliases in this
    module's own memoised walk costs one integer per field and closes both.
    """
    return [lambda: AddValidationRules([OperationLimitsRule])]
