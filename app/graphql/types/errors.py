import strawberry

from app.domain.errors import ValidationIssue


@strawberry.type
class ValidationErrorType:
    """Transport representation of a domain ValidationIssue."""

    field: str
    code: str
    message: str

    @classmethod
    def from_domain(cls, issue: ValidationIssue) -> "ValidationErrorType":
        return cls(
            field=issue.field,
            code=issue.code,
            message=issue.message,
        )
