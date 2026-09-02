import strawberry
from strawberry.fastapi import GraphQLRouter

from app.config import Environment
from app.graphql.context import get_context


def build_graphql_router(
    schema: strawberry.Schema,
    environment: Environment,
) -> GraphQLRouter:
    """Mount a schema as a FastAPI router.

    GraphiQL is served everywhere except production. The environment is
    passed in rather than read from settings so that this module, like the
    schema it wraps, imports without requiring configuration.
    """
    graphql_ide = None if environment == "production" else "graphiql"

    return GraphQLRouter(
        schema,
        context_getter=get_context,
        allow_queries_via_get=False,
        graphql_ide=graphql_ide,
    )
