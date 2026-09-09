"""Decide whether a `pytest -m db` run actually verified anything.

`pytest -m db` exits 0 in at least three situations that are not a passing
suite, and this script exists because every one of them has been mistaken for
one in this repository:

  * **Everything skipped.** `tests/conftest.py` answers an unreachable Docker
    daemon with `pytest.skip`, which is right on a laptop and wrong as
    evidence. A run reporting `6 passed, 970 skipped` exits 0 and has tested
    almost nothing.
  * **Nothing collected.** A path typo or a marker change collects zero tests
    and exits 0 having run none of them.
  * **The exit code never reached the reader.** `pytest ... | tail -8` reports
    `tail`'s status, not pytest's, so a run with real failures prints a
    zero exit code. That is not hypothetical -- a run with 16 failures and
    998 errors was read as green here for exactly that reason.

So the gate is asserted from the JUnit report and never from a process exit
code. The three numbers a reader needs -- passed, failed, skipped -- are
printed every time, whether the gate passes or fails, because "how many ran"
is the question a green line is supposed to answer and usually does not.

Usage, on one report or on several:

    python -m scripts.db_gate report.xml
    python -m scripts.db_gate shard_00.xml shard_01.xml shard_02.xml

Several matters on a machine that cannot hold the whole db suite in one
process. Sharding is a real answer to a memory ceiling, but it splits the
evidence across N files, and N files each reporting "0 failed" is not the
same claim as the corpus passing -- one shard silently collecting nothing
would go unnoticed. Aggregating here keeps the claim single: every shard ran
tests, and none of them failed, errored or skipped.

Acceptance is `failed == 0 and errors == 0 and skipped == 0 and total > 0`,
per report and in aggregate. `errors` is checked separately from `failures`
because a fixture that cannot build its schema raises rather than asserts,
and that is the shape an infrastructure collapse takes -- the 998 above were
errors, not failures.
"""

import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Counts:
    """What one JUnit report says about one run."""

    total: int
    failed: int
    errors: int
    skipped: int

    @property
    def passed(self) -> int:
        """Derived, and floored at zero.

        pytest counts an errored test inside `tests`, so the subtraction is
        normally exact. It is floored anyway because these four numbers come
        from a file this script does not write: a report whose `errors`
        overlaps `tests` differently -- another runner's, or a merged one --
        would otherwise print a negative "passed", which is a nonsense
        number in the one line a human actually reads. The gate itself never
        consults this; it reads the three raw counts.
        """
        return max(0, self.total - self.failed - self.errors - self.skipped)

    @property
    def clean(self) -> bool:
        """Whether this run is admissible as evidence.

        `total > 0` is part of it rather than a separate check: a report of
        nothing is not a clean run, it is an absent one, and the two read
        identically in a summary line.
        """
        return (
            self.total > 0
            and self.failed == 0
            and self.errors == 0
            and self.skipped == 0
        )

    def __add__(self, other: "Counts") -> "Counts":
        return Counts(
            total=self.total + other.total,
            failed=self.failed + other.failed,
            errors=self.errors + other.errors,
            skipped=self.skipped + other.skipped,
        )


def read_counts(path: Path) -> Counts:
    """The counts in a JUnit XML report.

    Handles both shapes pytest emits: a bare `<testsuite>` and a
    `<testsuites>` wrapper around one. Reading attributes rather than
    counting child elements, so this stays O(1) on a report with thousands
    of cases and does not depend on pytest's per-case markup.
    """
    root = ElementTree.parse(path).getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root

    if suite is None:
        raise SystemExit(f"no testsuite element in {path}")

    return Counts(
        total=int(suite.get("tests", "0")),
        failed=int(suite.get("failures", "0")),
        errors=int(suite.get("errors", "0")),
        skipped=int(suite.get("skipped", "0")),
    )


def _line(label: str, counts: Counts) -> str:
    return (
        f"{label:<28} passed={counts.passed:<5} failed={counts.failed:<4} "
        f"errors={counts.errors:<4} skipped={counts.skipped:<4} "
        f"total={counts.total}"
    )


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m scripts.db_gate <report.xml> [report.xml ...]")
        return 2

    reports = [Path(argument) for argument in argv]
    missing = [report for report in reports if not report.is_file()]

    if missing:
        # A missing report is the most dangerous input this script can be
        # given: it means a shard did not finish -- killed by the OOM
        # reaper, most likely -- and treating an absent file as an empty
        # pass would turn the exact failure this gate exists for into a
        # green line.
        for report in missing:
            print(f"MISSING {report}", file=sys.stderr)

        return 1

    aggregate = Counts(total=0, failed=0, errors=0, skipped=0)

    for report in reports:
        counts = read_counts(report)
        aggregate += counts

        if len(reports) > 1:
            print(
                _line(report.stem, counts)
                + ("" if counts.clean else "   <-- NOT CLEAN")
            )

    print(_line("AGGREGATE" if len(reports) > 1 else "db suite", aggregate))

    if not aggregate.clean:
        print(
            "\nFAIL: a db run is admissible only with failed=0, errors=0, "
            "skipped=0 and total>0.\n"
            "Skips here mean the fixture could not reach Docker or could not "
            "start pgvector/pgvector:pg18, which is an environment fact on a "
            "laptop and a broken gate as evidence.",
            file=sys.stderr,
        )

        return 1

    if any(not read_counts(report).clean for report in reports):
        # Unreachable while `clean` is a conjunction of non-negative counts,
        # and kept because that is a property of the current arithmetic
        # rather than of the contract: a future per-shard rule that the
        # aggregate cannot express would otherwise pass silently.
        print("\nFAIL: a shard was not clean.", file=sys.stderr)

        return 1

    print("\nOK: every db test ran, and none failed, errored or skipped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
