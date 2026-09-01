import strawberry


@strawberry.type
class PageInfo:
    has_next_page: bool
    end_cursor: str | None
