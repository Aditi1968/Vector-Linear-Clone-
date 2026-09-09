"""Diagnose a deployment database against this repository's migrations.

Read-only. Applies nothing, creates nothing, drops nothing -- not even the
ledger table, which `scripts/apply_migration.py --status` would create as its
prologue. This exists precisely so that a database can be looked at before
anybody decides whether writing to it is safe.

It NEVER prints a DSN, a password, a host or a username. What it prints is
the identity of the database (its name, version and schema), the state of the
`schema_migrations` ledger, and -- the part that matters for an adoption
decision -- a per-migration comparison between the objects each migration
file declares and the objects that actually exist.

Usage:

    python -m scripts.inspect_deployment_db                 # DATABASE_URL
    python -m scripts.inspect_deployment_db --env-file .env.staging

The env file is read for `DATABASE_URL` or `STAGING_DATABASE_URL`, in that
order of preference, and nothing else in it is touched.
"""

import argparse
import asyncio
import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"

VERSION_PATTERN = re.compile(r"^(\d+)_")

# What a migration file DECLARES. Deliberately five narrow patterns rather
# than a SQL parser: each one names an object that either exists in the
# catalogue or does not, which is the only question adoption has to answer.
DECLARES = {
    "table": re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?([a-z_][a-z0-9_]*)",
        re.IGNORECASE,
    ),
    "index": re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
        r"(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)",
        re.IGNORECASE,
    ),
    "constraint": re.compile(
        r"(?:ADD\s+)?CONSTRAINT\s+([a-z_][a-z0-9_]*)",
        re.IGNORECASE,
    ),
    "function": re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:public\.)?([a-z_][a-z0-9_]*)",
        re.IGNORECASE,
    ),
    "extension": re.compile(
        r"CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([a-z_][a-z0-9_]*)\"?",
        re.IGNORECASE,
    ),
}

# Objects a migration file names that a correctly-migrated database does NOT
# contain. Their absence is right, so counting them as missing would report a
# healthy database as PARTIAL -- which, in a tool whose whole job is deciding
# whether a schema may be adopted, is the most expensive kind of wrong.
#
# Each entry is a fact about PostgreSQL or about a later migration, verified
# rather than assumed, and each is here instead of being silently tolerated.
NEVER_MATERIALISES = {
    # migrations/002_tenancy.sql drops 001's keyset index and replaces it
    # with the workspace-leading one two statements later.
    "index": {"issues_created_at_id_idx"},
    # migrations/017_github_development.sql declares each of these as a
    # UNIQUE constraint over the column set that is ALREADY the table's
    # primary key. PostgreSQL discards a unique constraint identical to the
    # primary key rather than building a second index for it -- confirmed on
    # pgvector/pgvector:pg18 by declaring both and finding only the pkey in
    # pg_constraint. So these names exist in the file and in no database.
    #
    # Harmless: the comment above each one says the primary key is doing the
    # FK-target job, and it is -- `github_commit_issues` and the pull-request
    # link table reference the pkey's column set and resolve against it. The
    # declarations are redundant, not wrong.
    "constraint": {
        "github_commits_workspace_repository_sha_key",
        "github_pull_requests_workspace_repository_number_key",
    },
}


def _strip_sql_comments(sql: str) -> str:
    """Line and block comments out, so prose is not read as declarations.

    These files are heavily commented and several comments name objects --
    "without this every `DELETE FROM teams` scans issues" -- so reading them
    would invent declarations no statement makes.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def declared_objects(path: Path) -> dict[str, set[str]]:
    sql = _strip_sql_comments(path.read_text(encoding="utf-8"))

    found: dict[str, set[str]] = {}

    for kind, pattern in DECLARES.items():
        names = {match.lower() for match in pattern.findall(sql)}
        found[kind] = names - NEVER_MATERIALISES.get(kind, set())

    return found


async def live_objects(conn: asyncpg.Connection) -> dict[str, set[str]]:
    async def names(query: str) -> set[str]:
        return {record[0].lower() for record in await conn.fetch(query)}

    return {
        "table": await names(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ),
        "index": await names(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        ),
        "constraint": await names(
            """
            SELECT conname FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = 'public'
            """
        ),
        "function": await names(
            """
            SELECT proname FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
            """
        ),
        "extension": await names("SELECT extname FROM pg_extension"),
    }


def _read_env_file(path: Path) -> str | None:
    if not path.is_file():
        return None

    wanted = ("DATABASE_URL", "STAGING_DATABASE_URL")
    found: dict[str, str] = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()

        if key in wanted:
            found[key] = value.strip().strip("'\"")

    return found.get("DATABASE_URL") or found.get("STAGING_DATABASE_URL")


async def report(dsn: str) -> int:
    parts = urlsplit(dsn)
    host = parts.hostname or ""

    print("=" * 62)
    print("DEPLOYMENT DATABASE INSPECTION (read-only)")
    print("=" * 62)

    # Identity, without the endpoint id: enough to tell two databases apart,
    # not enough to reconstruct a connection string.
    #
    # sha256 rather than hash(): Python salts hash() per process, so the same
    # host would fingerprint differently on every run and the one question
    # this line exists to answer -- "is this the same host as the other one?"
    # -- could never be answered. Truncated to 12 hex characters, which is
    # far too little to attack and far more than enough to compare.
    suffix = ".".join(host.split(".")[-3:]) if host.count(".") >= 2 else host
    digest = hashlib.sha256(host.encode()).hexdigest()[:12]

    print(f"host suffix:              ...{suffix}")
    print(f"host fingerprint:         {digest}")

    conn = await asyncpg.connect(dsn, timeout=30)

    try:
        version = await conn.fetchval("SELECT version()")

        print(
            f"database_name:            {await conn.fetchval('SELECT current_database()')}"
        )
        print(f"postgres_version:         {version.split()[1]}")
        print(
            f"current_schema:           {await conn.fetchval('SELECT current_schema()')}"
        )
        print(f"current_user:             {await conn.fetchval('SELECT current_user')}")

        ledger = await conn.fetchval("SELECT to_regclass('public.schema_migrations')")
        exists = ledger is not None

        print(f"schema_migrations_exists: {str(exists).lower()}")

        recorded: list[str] = []

        if exists:
            rows = await conn.fetch(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
            recorded = [r["version"] for r in rows]

            print(f"schema_migrations_row_count: {len(recorded)}")
            print(
                f"highest_recorded_migration:  {recorded[-1] if recorded else '(none)'}"
            )
            print(f"recorded migration numbers:  {', '.join(recorded) or '(none)'}")
        else:
            print("schema_migrations_row_count: 0")
            print("highest_recorded_migration:  (no ledger table)")
            print("recorded migration numbers:  (no ledger table)")

        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        all_versions = [VERSION_PATTERN.match(f.name).group(1) for f in files]
        pending = [v for v in all_versions if v not in recorded]

        print(f"pending migration numbers:   {', '.join(pending) or '(none)'}")

        print()
        print("-" * 62)
        print("SCHEMA FINGERPRINT PER MIGRATION")
        print("-" * 62)
        print("  present = every object the file declares exists")
        print("  ABSENT  = none of them exists")
        print("  PARTIAL = some do and some do not -- ambiguous, never adopt")
        print()

        live = await live_objects(conn)
        verdicts: dict[str, str] = {}

        for path in files:
            version = VERSION_PATTERN.match(path.name).group(1)
            declared = declared_objects(path)

            total = sum(len(v) for v in declared.values())
            missing: list[str] = []
            present_count = 0

            for kind, names in declared.items():
                for name in sorted(names):
                    if name in live[kind]:
                        present_count += 1
                    else:
                        missing.append(f"{kind}:{name}")

            if total == 0:
                verdict = "no-objects"
            elif present_count == total:
                verdict = "present"
            elif present_count == 0:
                verdict = "ABSENT"
            else:
                verdict = "PARTIAL"

            verdicts[version] = verdict

            in_ledger = "ledger" if version in recorded else "  --  "
            flag = "" if verdict in ("present", "no-objects") else "   <<<"

            print(
                f"  {version}  {in_ledger}  {verdict:<10}"
                f" {present_count}/{total} objects{flag}"
            )

            if missing and verdict == "PARTIAL":
                for item in missing[:6]:
                    print(f"          missing: {item}")
                if len(missing) > 6:
                    print(f"          ... and {len(missing) - 6} more")

        print()
        print("-" * 62)
        print("ROWS PRESENT (non-empty tables only)")
        print("-" * 62)

        any_rows = False

        for table in sorted(live["table"]):
            count = await conn.fetchval(f'SELECT count(*) FROM public."{table}"')
            if count:
                any_rows = True
                print(f"  {table:<38} {count:>8}")

        if not any_rows:
            print("  (every table is empty)")

        print()
        print("-" * 62)
        print("VERDICT")
        print("-" * 62)

        unrecorded_but_present = [
            v
            for v, verdict in verdicts.items()
            if verdict == "present" and v not in recorded
        ]
        partial = [v for v, verdict in verdicts.items() if verdict == "PARTIAL"]

        if partial:
            print("  CASE B -- objects from these migrations are PARTIALLY present:")
            print(f"    {', '.join(partial)}")
            print("  A partial migration cannot be adopted: the file is all-or-")
            print("  nothing and half of it has been applied. This database was")
            print("  written to by something that was not the runner, or a")
            print("  branch was taken mid-statement.")
        elif unrecorded_but_present:
            print("  CASE C -- the schema is ahead of the ledger.")
            print(f"    present but unrecorded: {', '.join(unrecorded_but_present)}")
            print("  Every object these migrations declare exists, but no ledger")
            print("  row does -- so the runner will try to re-create them and")
            print("  fail on the first CREATE TABLE.")
        elif pending:
            print("  Ledger and schema agree; migrations genuinely remain.")
            print(f"    pending: {', '.join(pending)}")
        else:
            print("  Ledger and schema agree, and nothing is pending.")

        return 0
    finally:
        await conn.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        default=".env",
        help="file to read DATABASE_URL / STAGING_DATABASE_URL from",
    )
    args = parser.parse_args(argv)

    path = Path(args.env_file)

    if not path.is_absolute():
        path = REPO_ROOT / path

    dsn = _read_env_file(path)

    if not dsn:
        print(f"No DATABASE_URL or STAGING_DATABASE_URL found in {path.name}.")
        print("The value is never printed, logged or committed.")
        return 2

    return asyncio.run(report(dsn))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
