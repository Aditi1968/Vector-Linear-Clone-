import re
from dataclasses import dataclass
from typing import Final

from app.domain.issues import IssueEntity
from app.domain.projects import ProjectEntity


# `ENG-42`, and nothing looser.
#
# The two halves are the constraints the schema already declares, restated
# here so that a string which cannot possibly name an issue never becomes a
# query: `teams_key_format` in migrations/005_team_workflows.sql is
# `^[A-Z][A-Z0-9]{0,9}$` -- which is why the key half forbids a hyphen and
# therefore why `ENG-42` has exactly one parse -- and `issues_number_positive`
# is `number >= 1`.
#
# Eighteen digits, because `issues.number` is BIGINT. A longer run of digits
# is not a large issue number, it is an int that asyncpg would refuse to bind;
# refusing it here makes that a miss rather than an exception.
#
# Case-insensitive on the key and uppercased before use. A user typing
# `eng-42` means the same issue, and normalising here is safe precisely
# because the stored form has one legal spelling.
_IDENTIFIER = re.compile(r"^([A-Za-z][A-Za-z0-9]{0,9})-([0-9]{1,18})$")

# The longest query string the search accepts.
#
# A bound on work, not a product rule: `websearch_to_tsquery` parses whatever
# it is handed, so an unbounded argument is an unbounded parse on a public
# field. Set far above any real search box entry.
QUERY_MAX_LENGTH: Final = 200


def parse_issue_identifier(query: str) -> tuple[str, int] | None:
    """`ENG-42` split into a team key and a number, or None.

    None means "this is not an identifier", which is the ordinary case: it is
    what every prose query answers. The caller runs a full-text search either
    way; this only decides whether a second, exact lookup is worth making.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """
    match = _IDENTIFIER.match(query.strip())

    if match is None:
        return None

    return match.group(1).upper(), int(match.group(2))


@dataclass(frozen=True, slots=True)
class SearchResults:
    """What one search found, per kind.

    Two typed lists rather than one list of a union. A client renders issues
    and projects differently -- different columns, different links -- so a
    heterogeneous list would be sorted into these two on arrival anyway, and
    a single relevance ordering across kinds would be comparing two `ts_rank`
    scores computed over different documents, which is a number without a
    meaning.

    No `has_next_page` and no cursor. Search is a top-N over a relevance
    ordering that changes whenever a row does, so a cursor into it would
    resume a page walk against an ordering that no longer exists. A caller
    that wants more asks for a larger `first` or a better query.
    """

    issues: list[IssueEntity]
    projects: list[ProjectEntity]
