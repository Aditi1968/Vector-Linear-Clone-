import strawberry
from strawberry.extensions import DisableIntrospection
from strawberry.extensions.base_extension import SchemaExtension

from app.config import settings
from app.graphql.mutations.issues import Mutation
from app.graphql.queries.issues import Query


extensions: list[type[SchemaExtension] | SchemaExtension] = []

if settings.environment == "production":
    # strawberry-graphql 0.326.0 has no StrawberryConfig(disable_introspection=...);
    # the supported mechanism for this version is the DisableIntrospection extension.
    extensions.append(DisableIntrospection())

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=extensions,
)
