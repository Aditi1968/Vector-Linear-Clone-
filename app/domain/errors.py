from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    field: str
    code: str
    message: str


class ValidationError(Exception):
    """An expected input validation failure.

    The structured `issues` collection is the contract; the exception
    message stays generic on purpose so that nothing downstream is tempted
    to parse it. Pure application code -- no Strawberry, FastAPI, asyncpg
    or PostgreSQL.
    """

    def __init__(self, issues: list[ValidationIssue]):
        super().__init__("Validation failed")

        self.issues = issues
