from enum import Enum

import strawberry

from app.domain.health import HEALTH_VALUES


@strawberry.enum(name="Health")
class HealthType(Enum):
    """How a project or an initiative is going, as an enum rather than a String.

    ONE enum for both, because there is one vocabulary: a `ProjectHealth` and
    an `InitiativeHealth` differing only in name would be two types a client
    has to map between to render one badge, and two places for a fourth value
    to be added to one of.

    An enum is the difference between a client discovering the legal values by
    reading the schema and discovering them by sending one and being told no.
    It also moves the check to GraphQL validation, which runs before any
    resolver, so an unknown health never reaches a service or a connection.

    The members' *values* are the strings the database stores, and the members'
    *names* are what appears in SDL -- so the wire contract is `ON_TRACK` while
    the column holds `on_track`. Keeping both spellings in one place is what
    stops the mapping being an `if` ladder somewhere.

    Not generated from `HEALTH_VALUES`, for the reason `ProjectStateType` gives
    about `PROJECT_STATES`: a dynamically built enum has no names for a type
    checker or an editor to know about, and this is a contract that should be
    greppable.
    """

    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    OFF_TRACK = "off_track"


# Checked at import time rather than left to a test, exactly as
# `ProjectStateType` is. The enum above and `HEALTH_VALUES` are two spellings
# of four CHECK constraints in migrations/022_initiatives.sql, and a
# disagreement between them is not a failing feature -- it is a payload that
# cannot be built, raised from whichever resolver happens to read the drifted
# row first, in production, as a masked internal error. Failing at import turns
# that into a process that will not start.
#
# An `if`/`raise` and not an `assert`, because `python -O` discards asserts and
# this is a startup gate rather than a debugging aid.
if tuple(member.value for member in HealthType) != HEALTH_VALUES:
    raise RuntimeError(
        "HealthType and app.domain.health.HEALTH_VALUES disagree; they are "
        "both statements of the health CHECK constraints in "
        "migrations/022_initiatives.sql and have to be changed together"
    )
