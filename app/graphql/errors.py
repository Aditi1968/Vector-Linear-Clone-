from graphql import GraphQLError

from app.domain.errors import ValidationError


# The one code `app.graphql.schema.PUBLIC_ERROR_CODES` publishes. Raising an
# error under it is a deliberate act of telling the client something; anything
# without it is masked as "Internal server error" on the way out.
BAD_USER_INPUT = "BAD_USER_INPUT"


def bad_user_input(message: str, error: ValidationError) -> GraphQLError:
    """A domain ValidationError as an error a client is allowed to read.

    For fields with no payload of their own. A mutation returning
    `payload.errors` has somewhere structured to put a rejection; a query
    field returning a connection does not, so its only channel is the
    top-level errors array -- which is masked unless the error carries a
    published code.

    The structured issues ride in `extensions` in the same shape
    `ValidationErrorType` gives them, so a client parses one vocabulary
    whichever channel a rejection arrives through.

    Raise this `from None`. The exception it describes is fully represented
    by the message and the extensions, and chaining it would attach an
    `original_error` -- which is exactly what `is_public_error` reads to
    decide that an error is an accident and must be masked.
    """
    return GraphQLError(
        message,
        extensions={
            "code": BAD_USER_INPUT,
            "issues": [
                {
                    "field": issue.field,
                    "code": issue.code,
                    "message": issue.message,
                }
                for issue in error.issues
            ],
        },
    )
