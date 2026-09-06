from typing import Final


class UnsetType:
    """The absence of a field in a partial update.

    A partial update has three cases per field, not two: leave it alone, set
    it to a value, and -- for a nullable field -- clear it. `None` can only
    express two of them, so a service taking `description: str | None = None`
    cannot tell "the client did not mention description" from "the client
    asked to clear it", and will silently pick one. Which one it picks is
    invisible at the call site and wrong half the time.

    This is the third case, spelled as a value so it can travel through an
    ordinary argument. `UNSET` means the caller said nothing; `None` means
    the caller said null.

    Deliberately not `strawberry.UNSET`, which exists and does the same job
    one layer up. Services and domain code must not import Strawberry -- see
    CLAUDE.md -- so the GraphQL layer translates its sentinel into this one
    at the boundary, exactly as it translates every other transport type.

    Falsy on purpose: `if name:` is not how a caller should test for it (an
    empty string is a value, and `UNSET` is not), but a sentinel that read as
    true in a boolean context would make that mistake silent rather than
    merely possible. The test that is correct is `is UNSET` / `is not UNSET`,
    which is why this class is never instantiated a second time.

    Callers use both `x is UNSET` and `isinstance(x, UnsetType)`, and the
    difference is not stylistic. The two are equivalent at runtime, but mypy
    narrows a union only through the isinstance form -- an identity test
    against a module-level constant tells it nothing -- so the isinstance
    spelling appears wherever the value is about to be used as its real type,
    and the shorter one wherever the answer is only a boolean.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Final = UnsetType()
