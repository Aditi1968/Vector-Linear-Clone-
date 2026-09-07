"""How a piece of work is going, in the one vocabulary both features use.

A module of its own rather than a constant on either side, because `projects`
and `initiatives` are two tables carrying the same three values and neither
owns the word. Putting it in `app.domain.projects` would make
`app.domain.initiatives` import the projects domain to say how an initiative is
doing, and vice versa; the shared thing is the vocabulary, so the vocabulary
is what gets the module.

Pure application code -- no Strawberry, FastAPI or asyncpg.
"""

from typing import Final


# The three values `projects_health_check`, `initiatives_health_check`,
# `project_updates_health_check` and `initiative_updates_health_check` all
# admit, worst-last in the order a report reads.
#
# This tuple and those four constraints are five statements of one rule and
# have to change together. That is not a duplication that can be designed away:
# the database has to reject an unknown health whoever writes it, and the
# services have to reject one without spending a round trip and without turning
# a CheckViolationError into a user-facing message.
# tests/test_migration_022_db.py asserts they agree.
HEALTH_VALUES: Final[tuple[str, ...]] = ("on_track", "at_risk", "off_track")
