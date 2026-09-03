from typing import TYPE_CHECKING

import strawberry
from strawberry.fastapi import GraphQLRouter

from app.config import Environment
from app.graphql.context import get_context


if TYPE_CHECKING:
    # Type-checking only. GraphQL_IDE is a typing.Literal alias with no
    # runtime role, and `strawberry.http.ides` is an internal module path --
    # importing it at runtime would put a symbol the application never uses
    # on create_app()'s import path, where a strawberry release that moved
    # it would break startup rather than type checking.
    from strawberry.http.ides import GraphQL_IDE


def build_graphql_router(
    schema: strawberry.Schema,
    environment: Environment,
) -> GraphQLRouter:
    """Mount a schema as a FastAPI router.

    GraphiQL is served everywhere except production. The environment is
    passed in rather than read from settings so that this module, like the
    schema it wraps, imports without requiring configuration.
    """
    graphql_ide: "GraphQL_IDE | None" = (
        None if environment == "production" else "graphiql"
    )

    return GraphQLRouter(
        schema,
        context_getter=get_context,
        allow_queries_via_get=False,
        graphql_ide=graphql_ide,
    )
