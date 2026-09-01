import strawberry


@strawberry.input
class IssueCreateInput:
    title: str
    description: str | None = None
    priority: int = 0
