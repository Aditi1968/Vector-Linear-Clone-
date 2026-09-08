import strawberry
from strawberry.types import Info

from app.domain.analytics import ANALYTICS_DEFAULT_DAYS, ANALYTICS_MAX_DAYS
from app.domain.errors import ValidationError
from app.graphql.errors import bad_user_input
from app.graphql.scope import authorized_scope
from app.graphql.types.analytics import WorkspaceAnalyticsType


@strawberry.type
class AnalyticsQuery:
    """The analytics read, merged into the root Query by app.graphql.schema."""

    @strawberry.field(
        description=(
            "How this workspace has been moving over the last `days` days, "
            f"which must be between 1 and {ANALYTICS_MAX_DAYS}: a larger "
            "window is refused rather than quietly shortened. One aggregate "
            "rather than a field per metric, because they share a window and "
            "a tenant and are rendered together -- separate root fields would "
            "let one document ask for six different windows at once."
        )
    )
    async def workspace_analytics(
        self,
        info: Info,
        workspace_slug: str,
        days: int = ANALYTICS_DEFAULT_DAYS,
    ) -> WorkspaceAnalyticsType:
        """Throughput, cycle time and distribution for one workspace.

        `days` is the only argument in this schema that sets a response's
        cardinality without being a page size, so it is the one the operation
        limits cannot price: `app/graphql/limits.py` measures a document during
        validation, where a variable has no value, and this field declares no
        `first` for it to read. The ceiling is therefore enforced in
        `AnalyticsService.overview` -- ANALYTICS_MAX_DAYS, one point per day --
        and a larger value is refused as BAD_USER_INPUT rather than clamped, so
        that no response is ever labelled with a window it does not cover.

        Only that one expected refusal is translated. An asyncpg failure or a
        bug propagates and is masked by app/graphql/schema.py.

        A workspace the viewer does not belong to and one that does not exist
        are the same NOT_FOUND, from `authorized_scope`. There is no partial
        answer for a member with limited reach: this schema has no FORBIDDEN
        code and no per-team read permission, so a member sees the whole
        workspace's numbers or none of them.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            overview = await info.context.analytics_service.overview(
                scope=scope,
                days=days,
            )
        except ValidationError as exc:
            # Only the expected `days` refusal. Anything else -- an asyncpg
            # failure, a bug -- propagates as a real execution error and is
            # masked on the way out.
            raise bad_user_input("Invalid analytics window", exc) from None

        return WorkspaceAnalyticsType.from_domain(overview)
