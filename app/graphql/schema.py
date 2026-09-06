from collections.abc import Callable

import strawberry
from graphql import GraphQLError
from strawberry.extensions import DisableIntrospection, MaskErrors
from strawberry.extensions.base_extension import SchemaExtension
from strawberry.tools import merge_types

from app.config import Environment
from app.graphql.limits import operation_limit_extensions
from app.graphql.mutations.auth import AuthMutation
from app.graphql.mutations.comments import CommentMutation
from app.graphql.mutations.cycles import CycleMutation
from app.graphql.mutations.github import GithubMutation
from app.graphql.mutations.issues import Mutation as IssueMutation
from app.graphql.mutations.labels import LabelMutation
from app.graphql.mutations.memberships import MembershipMutation
from app.graphql.mutations.projects import ProjectMutation
from app.graphql.mutations.relations import RelationMutation
from app.graphql.mutations.slack import SlackMutation
from app.graphql.queries.auth import AuthQuery
from app.graphql.queries.cycles import CycleQuery
from app.graphql.queries.github import GithubQuery
from app.graphql.queries.issues import Query as IssueQuery
from app.graphql.queries.labels import LabelQuery
from app.graphql.queries.memberships import MembershipQuery
from app.graphql.queries.projects import ProjectQuery
from app.graphql.queries.search import SearchQuery
from app.graphql.queries.slack import SlackQuery
from app.graphql.queries.teams import TeamQuery


# The two root types, assembled from one class per feature, rather than a
# single growing `Query` class every feature has to edit. The GraphQL root is
# a junction by nature -- everything the API exposes hangs off it -- and a
# junction that is also a file is a file every branch conflicts in.
#
# `merge_types` rather than plain inheritance for one property: it counts the
# field names it is merging and warns on a collision instead of silently
# letting the first class in the tuple win. Two features that both define a
# root field called `me` or `issues` is a mistake nobody would see in a
# schema that still builds, and this is a repository where several people add
# root fields at once.
#
# Tuple order is SDL field order, so it stays stable across exports and
# `frontend/schema.graphql` does not churn.
#
# These two calls are the ONLY definition of the root types. A second
# `class Query(...)` anywhere below them silently shadows the merge -- the name
# is simply rebound, `build_schema` picks up whichever came last, and the
# fields of every feature not named in that class vanish from the API with the
# whole suite still green. This has happened here once already. Add a feature
# by extending the tuples, never by declaring another root.
# Named rather than passed inline so that a test can ask the question this
# file's comment can only assert: does every field of every type below still
# reach the root the schema is built from. See
# tests/test_root_composition.py.
QUERY_TYPES = (
    IssueQuery,
    AuthQuery,
    TeamQuery,
    MembershipQuery,
    LabelQuery,
    CycleQuery,
    ProjectQuery,
    SearchQuery,
    GithubQuery,
    SlackQuery,
)

Query = merge_types("Query", QUERY_TYPES)
MUTATION_TYPES = (
    IssueMutation,
    AuthMutation,
    MembershipMutation,
    LabelMutation,
    CommentMutation,
    CycleMutation,
    ProjectMutation,
    RelationMutation,
    GithubMutation,
    SlackMutation,
)

Mutation = merge_types("Mutation", MUTATION_TYPES)


# The public error vocabulary. An error reaches a client with its own
# message only if a resolver deliberately raised a GraphQLError carrying
# one of these codes; everything else is an accident and is masked.
#
# Adding a code here publishes every message that will ever be raised under
# it, so a new entry is a decision about what clients may be told -- not a
# convenience for surfacing a message that happens to be useful in a log.
#
# UNAUTHENTICATED and NOT_FOUND are published because a client cannot act on
# a masked error: "Internal server error" gives a browser no reason to send
# the user to a login screen, and no reason to stop retrying. Both are safe
# to publish because the messages raised under them are fixed strings that
# describe the request rather than the server. There are exactly two of them:
# `app.graphql.viewer.UNAUTHENTICATED_MESSAGE`, raised wherever a request must
# have an identity, and `app.graphql.queries.memberships`'s NOT_FOUND, which is
# deliberately the same message for a workspace that does not exist and one the
# viewer may not see.
PUBLIC_ERROR_CODES = frozenset({"BAD_USER_INPUT", "UNAUTHENTICATED", "NOT_FOUND"})

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
