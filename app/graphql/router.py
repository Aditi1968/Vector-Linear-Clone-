from strawberry.fastapi import GraphQLRouter

from app.config import settings
from app.graphql.context import get_context
from app.graphql.schema import schema


graphql_ide = None if settings.environment == "production" else "graphiql"

graphql_router = GraphQLRouter(
    schema,
    context_getter=get_context,
    allow_queries_via_get=False,
    graphql_ide=graphql_ide,
)
