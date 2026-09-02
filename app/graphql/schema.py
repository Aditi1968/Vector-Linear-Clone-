import strawberry
from strawberry.extensions import DisableIntrospection
from strawberry.extensions.base_extension import SchemaExtension

from app.config import Environment
from app.graphql.mutations.issues import Mutation
from app.graphql.queries.issues import Query


def build_schema(environment: Environment) -> strawberry.Schema:
    """Build the GraphQL schema for a given environment.

    The environment is a parameter rather than a settings lookup so that
    building a schema never requires a configured database. Tests build a
    "test" schema directly; the composition root passes the real value.

    The hardening policy itself stays here, next to the schema it protects,
    rather than being decided by the caller.
    """
    extensions: list[type[SchemaExtension] | SchemaExtension] = []

    if environment == "production":
        # strawberry-graphql 0.326.0 has no StrawberryConfig(disable_introspection=...);
        # the supported mechanism for this version is the DisableIntrospection extension.
        #
        # The class, not an instance: passing an instance is deprecated in
        # 0.326.0 and slated for removal. Since this is the switch that hides
        # the schema in production, a Strawberry upgrade dropping instance
        # support would have silently taken introspection protection with it.
        extensions.append(DisableIntrospection)

    return strawberry.Schema(
        query=Query,
        mutation=Mutation,
        extensions=extensions,
    )
