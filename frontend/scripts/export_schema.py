"""Print the backend's GraphQL schema as SDL, on stdout.

This is the single source of `frontend/schema.graphql`. It builds the real
Strawberry schema -- the same `build_schema` the ASGI app builds -- rather
than reading a checked-in copy, so the SDL the frontend generates types from
cannot describe a server that does not exist.

## How to run it

Output goes to **stdout**. There is no `--out` flag and no other option; the
caller redirects. From the repository root:

    PYTHONPATH=. python frontend/scripts/export_schema.py > frontend/schema.graphql

`PYTHONPATH` is not optional and not a preference. Python puts the *script's
own directory* on `sys.path`, so without it `import app` resolves against
`frontend/scripts/` and fails no matter which directory the command is run
from. `frontend/scripts/graphql-schema.mjs` exists to supply that (and the
working directory) for `npm run graphql:schema`; it is a convenience wrapper
around this exact invocation, not a second implementation.

Unknown arguments are rejected rather than ignored -- see `main`.

## Why this can run without a database

`build_schema` takes its environment as an argument instead of reading
settings, and nothing in the type or resolver modules touches a connection at
import time. So this imports `app.graphql.schema` with `DATABASE_URL` and
`ENVIRONMENT` both unset and still prints a schema. That is a property worth
stating because it is what makes the drift gate cheap enough to run
everywhere: no container, no Neon, no credentials, no `.env`.

## Why the output goes to stdout as bytes

`sys.stdout` is a text stream, and on Windows a text stream rewrites every
`\n` to `\r\n` on the way out. The caller compares this output against a file
byte for byte, so the translation would report drift on a schema that had not
changed. Writing to `sys.stdout.buffer` bypasses it.
"""

import argparse
import sys

from app.graphql.schema import build_schema


# The environment the schema is built for.
#
# It does not affect the SDL: the environment only selects schema
# *extensions* (error masking, operation limits, and `DisableIntrospection`
# in production), and an extension changes how operations are executed, not
# what types the schema declares. Verified by exporting all three and
# diffing. "development" is named here because it is the environment the
# frontend is developed against, and because it is the one value that is true
# whether or not anything is configured.
SCHEMA_ENVIRONMENT = "development"


def as_committed(sdl: str) -> str:
    """The SDL exactly as it must appear in a committed file.

    Two normalisations, and both exist for the same reason: `.pre-commit-
    config.yaml` runs `trailing-whitespace` and `end-of-file-fixer` over
    everything outside `migrations/`, so those hooks *will* rewrite
    `frontend/schema.graphql` after it is written. The drift gate then
    regenerates the file and diffs it against the committed bytes.

    If this function did not do what the hooks do, the two would disagree on
    the very first commit and the gate would be red forever, on a schema
    nobody had touched -- the worst kind of failing check, because the
    correct response to it is to ignore it.

    `as_str()` ends without a newline, so the second normalisation is not
    hypothetical. The first is insurance: no description in this schema
    currently ends a line in whitespace, and if one ever does, the hook will
    strip it and this has to strip it too.
    """
    lines = [line.rstrip() for line in sdl.split("\n")]

    return "\n".join(lines).rstrip("\n") + "\n"


def export_sdl() -> str:
    """The schema as SDL, ready to be written to a file verbatim."""
    return as_committed(build_schema(SCHEMA_ENVIRONMENT).as_str())


def main(argv: list[str] | None = None) -> int:
    """Write the SDL to stdout.

    The parser declares no arguments, which is the point: it is here so that
    an unrecognised flag exits non-zero with a message instead of being
    silently discarded. A script that accepts `--out somewhere` and writes
    nothing is a gate that reports success having done nothing, which is the
    failure this whole phase is built to prevent.
    """
    argparse.ArgumentParser(
        description="Print the backend GraphQL schema as SDL on stdout.",
        epilog=(
            "Output goes to stdout; redirect it. From the repository root: "
            "PYTHONPATH=. python frontend/scripts/export_schema.py "
            "> frontend/schema.graphql"
        ),
    ).parse_args(argv)

    sys.stdout.buffer.write(export_sdl().encode("utf-8"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
