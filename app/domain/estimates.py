"""What an estimate COUNTS, and which whole numbers a team may write.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.

migrations/006_issue_fields.sql stored `issues.estimate INTEGER` and refused to
say what the number means:

    "Any ceiling here would be a guess at what unit a team estimates in: 21 is
     absurd in hours and ordinary in points [...] A limit the product wants is
     a product policy, enforced where the product knows the team's unit."

This module is that policy, and `teams.estimate_scale` is where the unit is
recorded. The refusal was right and stays right: the schema still declines to
bound the column, because the bound depends on a value in another table.
"""

from enum import StrEnum
from typing import Final


class EstimateScale(StrEnum):
    """The unit one team's estimates are in.

    A StrEnum because the member IS the stored spelling, so nothing converts at
    the repository boundary and a scale cannot reach the database in a casing
    `teams_estimate_scale_known` refuses. The same move
    `WorkflowStateCategory` makes.

    The application's copy of that CHECK, for the reason `ActivityKind` gives
    about keeping one: the database has to refuse an unknown scale whoever
    writes it, and this code has to know the vocabulary without asking the
    database. `tests/test_estimates.py` pins the two equal.
    """

    # No unit. Any whole number the team has agreed on offline -- which is
    # exactly what every estimate written before migration 029 meant, and is
    # why this is the column's default. A team that never opens the setting
    # loses nothing.
    NONE = "none"

    # Relative effort, and clock time. Both unbounded above, for 006's reason:
    # a ceiling would be a guess, and pinning a Fibonacci ladder here would
    # make a team that already estimates 4 and 6 unable to edit its own issues.
    POINTS = "points"
    HOURS = "hours"

    # XS through XL, stored as 1..5. The one scale that genuinely constrains:
    # the integer is an INDEX into a fixed ladder rather than a quantity, so 8
    # is not a large t-shirt -- it is not a t-shirt at all.
    TSHIRT = "tshirt"


# The t-shirt ladder, smallest first, indexed from 1.
#
# A tuple and not a dict, because the stored integer IS the position: 1 is the
# first label and 5 is the fifth, so the mapping is `TSHIRT_LABELS[n - 1]` and
# the valid range falls out of `len` rather than being written a second time
# beside it. A dict would be two things -- the labels and the legal keys --
# that could disagree.
#
# Rendering is the client's, not this module's: nothing here builds a string
# for a screen. What this constant exists for is to say how many rungs the
# ladder has, and to give whoever renders it one place to read the names from.
TSHIRT_LABELS: Final = ("XS", "S", "M", "L", "XL")


# The smallest estimate any scale admits.
#
# Zero, not one, and the reason is 006's: "Zero does [have a meaning] -- 'we
# looked at this and it is free' -- so the bound is 0 and not 1."
# `issues_estimate_non_negative` is the floor under this for every write,
# including the ones that do not come through a service.
ESTIMATE_MIN: Final = 0


def is_valid_estimate(scale: EstimateScale, estimate: int) -> bool:
    """Whether this team may write this number as an estimate.

    Three of the four scales admit every non-negative integer and differ only
    in what a client renders beside the number, which is not a defect in the
    design -- it is 006's argument surviving contact with three real units. The
    fourth is the one that makes this function worth having.

    TSHIRT is a LADDER: the stored integer is a position, so the valid set is
    exactly 1..len(TSHIRT_LABELS) and zero is excluded along with everything
    above five. Zero is excluded deliberately and it is the one place this
    disagrees with `ESTIMATE_MIN` -- "free" is a quantity, and a ladder of
    sizes has no rung for it. A team that wants to record free work is not on a
    t-shirt scale.

    Deliberately NOT called with the scale a caller supplied. Every call site
    reads the scale out of `teams.estimate_scale` for the team that owns the
    issue; a scale that travelled with the request would let a client pick the
    rule its own value passes.
    """
    if estimate < ESTIMATE_MIN:
        return False

    if scale is EstimateScale.TSHIRT:
        return 1 <= estimate <= len(TSHIRT_LABELS)

    return True


def estimate_error_message(scale: EstimateScale) -> str:
    """What to tell somebody whose estimate this scale will not take.

    One sentence per scale rather than one generic one, because the two
    failures are different mistakes: below zero is a typo, and 8 on a t-shirt
    scale is somebody estimating in the unit their old team used. A message
    that said only "invalid estimate" would leave the second person to guess
    that the setting exists at all.

    Here rather than in the service, so that the wording sits next to the rule
    it describes -- and so that a scale added to the enum without a message is
    a KeyError in a test rather than a sentence that renders as "None".
    """
    if scale is EstimateScale.TSHIRT:
        return (
            "This team estimates in t-shirt sizes: "
            f"1-{len(TSHIRT_LABELS)} for {', '.join(TSHIRT_LABELS)}"
        )

    return f"Estimate must be {ESTIMATE_MIN} or greater"
