from collections.abc import Callable

import strawberry
from graphql import GraphQLError
from strawberry.extensions import DisableIntrospection, MaskErrors
from strawberry.extensions.base_extension import SchemaExtension

from app.config import Environment
from app.graphql.limits import operation_limit_extensions
from app.graphql.mutations.issues import Mutation
from app.graphql.queries.issues import Query


# The public error vocabulary. An error reaches a client with its own
# message only if a resolver deliberately raised a GraphQLError carrying
# one of these codes; everything else is an accident and is masked.
#
# Adding a code here publishes every message that will ever be raised under
# it, so a new entry is a decision about what clients may be told -- not a
# convenience for surfacing a message that happens to be useful in a log.
PUBLIC_ERROR_CODES = frozenset({"BAD_USER_INPUT"})

# What a masked error says. Deliberately uninformative: an attacker must
# not be able to tell a constraint violation from a connection failure from
# a bug by reading the response.
MASKED_ERROR_MESSAGE = "Internal server error"


def is_public_error(error: GraphQLError) -> bool:
    """Whether this error's own message may be shown to the client.

    Two kinds of error are public:

    * Errors with no `original_error`. graphql-core raises these itself
      while parsing or validating, and they describe the client's own
      document -- a syntax error, an unknown field, a broken operation
      limit. They contain nothing of the server.
    * Errors a resolver raised on purpose, tagged with a code from
      PUBLIC_ERROR_CODES. Raising one is the explicit act of publishing a
      message; the code is what distinguishes it from an exception that
      merely happened to reach the top.

    Anything else -- an asyncpg failure, a KeyError, a bug -- is internal,
    whatever its message says.
    """
    if error.original_error is None:
        return True

    return (error.extensions or {}).get("code") in PUBLIC_ERROR_CODES


def _should_mask_error(error: GraphQLError) -> bool:
    return not is_public_error(error)


# strawberry's own anonymisation, borrowed rather than reimplemented so that
# a masked error looks identical whichever of the two layers below produced
# it. It keeps the nodes, source, positions and path -- which describe the
# client's document -- and drops the message, the extensions and the
# originating exception.
_anonymise = MaskErrors(error_message=MASKED_ERROR_MESSAGE).anonymise_error


def _mask_result(result):
    """Mask any internal error that reached the caller unmasked.

    The `MaskErrors` extension runs inside strawberry's operation context
    (`strawberry/schema/schema.py:884`) and rewrites whatever it finds on
    `execution_context.result`. An exception raised while *validating* never
    gets there: `_prepare_operation_async` wraps parsing in `except
    Exception` (schema.py:699) but leaves `_run_validation` bare
    (schema.py:712-718), so a validation rule that raises propagates out of
    the operation context to the handler at schema.py:912, which coerces it
    with `GraphQLError(str(error), original_error=error)` (schema.py:230-233)
    and only then assigns `context.result` (schema.py:784) -- by which point
    the extension has already run against a result that was still None. The
    exception's own text went out verbatim.

    So the extension covers execution and streaming, and this covers the way
    out. Masking twice is harmless: an already-masked error has no
    `original_error`, so it reads as public here and is left alone.
    """
    if result.errors:
        result.errors = [
            _anonymise(error) if _should_mask_error(error) else error
            for error in result.errors
        ]

    return result


class _MaskedSchema(strawberry.Schema):
    """A schema that masks internal errors on every path out, not most.

    Overriding the two public entry points rather than reaching into
    strawberry's internals: whatever happens in between, nothing leaves
    without passing the same predicate. Subscriptions are not overridden
    because this schema declares none; `MaskErrors` still covers streamed
    results if one is ever added.
    """

    async def execute(self, *args, **kwargs):
        return _mask_result(await super().execute(*args, **kwargs))

    def execute_sync(self, *args, **kwargs):
        return _mask_result(super().execute_sync(*args, **kwargs))


def build_schema(environment: Environment) -> strawberry.Schema:
    """Build the GraphQL schema for a given environment.

    The environment is a parameter rather than a settings lookup so that
    building a schema never requires a configured database. Tests build a
    "test" schema directly; the composition root passes the real value.

    The hardening policy itself stays here, next to the schema it protects,
    rather than being decided by the caller.

    Masking and the operation limits apply in every environment, including
    development: an error that leaks internals in production leaks them
    because someone wrote the resolver that way, and a limit that only
    exists in production is a limit nobody notices breaking a client until
    it is deployed.

    Masking is applied twice over, by the extension and again by the schema
    class, because the extension alone does not see every way out. See
    `_mask_result`.
    """
    extensions: list[type[SchemaExtension] | Callable[[], SchemaExtension]] = [
        # A factory, not an instance: strawberry 0.326.0 deprecates passing
        # a built extension and calls the callable once per request.
        lambda: MaskErrors(
            should_mask_error=_should_mask_error,
            error_message=MASKED_ERROR_MESSAGE,
        ),
        *operation_limit_extensions(),
    ]

    if environment == "production":
        # strawberry-graphql 0.326.0 has no StrawberryConfig(disable_introspection=...);
        # the supported mechanism for this version is the DisableIntrospection extension.
        #
        # The class, not an instance: passing an instance is deprecated in
        # 0.326.0 and slated for removal. Since this is the switch that hides
        # the schema in production, a Strawberry upgrade dropping instance
        # support would have silently taken introspection protection with it.
        extensions.append(DisableIntrospection)

    return _MaskedSchema(
        query=Query,
        mutation=Mutation,
        extensions=extensions,
    )
