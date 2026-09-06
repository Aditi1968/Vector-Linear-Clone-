"""Fill a local demo database with enough work to look like a product.

`run-vector-local.ps1` calls this once the backend answers /healthz, so the
browser it opens lands on a populated issue list rather than an empty state.

Every write goes through the same GraphQL API the frontend uses -- `register`,
`issueCreate`, `issueUpdate` -- and never through a repository, a service
import or an INSERT. That is the whole point of seeding this way: a seed that
wrote rows directly would drift from the real write path the first time a
service grew a rule, and would stop proving that the path works at all. It
also means this file needs no database credentials, and has none.

The workspace, the team and its five workflow states are deliberately NOT
created here. `migrations/002_tenancy.sql` bootstraps the `vector` workspace
and its team, and `migrations/005_team_workflows.sql` gives every team a
Backlog/Todo/In Progress/Done/Canceled board. Seeding them again would be a
second definition of rows the migration chain already owns.

Idempotent by observation rather than by a ledger of its own: a workspace that
already has an issue is left exactly as it is, and an email that is already
registered is logged into instead. Running the launcher twice does not produce
two of anything.

LOCAL DEMO ONLY. The account below is a throwaway with a password published in
this file, in a database `run-vector-local.ps1 -Fresh` destroys and rebuilds.
It carries no real data and is worth nothing to anyone. The endpoint guard
below is what keeps it that way: this script refuses to run against anything
but a loopback address, so it cannot seed a deployed environment even if it is
handed one by mistake.

Usage:
  python -m scripts.seed_local_demo --api-url http://127.0.0.1:8000/graphql
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from urllib.parse import urlparse


# The demo account. A throwaway for a disposable local database -- see the
# module docstring. Defined here and printed from here, so the launcher does
# not carry a second copy that can disagree with this one.
DEMO_EMAIL = "demo@vector.local"
DEMO_PASSWORD = "vector-local-demo"
DEMO_NAME = "Demo User"

# The tenant `app/graphql/tenancy.py` binds every request to today, and the
# one migration 002 creates.
WORKSPACE_SLUG = "vector"

# The only addresses this script will talk to. `urlparse().hostname` lowercases
# and strips the brackets from an IPv6 literal, so `[::1]` arrives as `::1`.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

REQUEST_TIMEOUT_SECONDS = 30

# Returned by `register` when the address is taken. The one case that is not a
# failure here: it means a previous run already made this account.
EMAIL_TAKEN = "EMAIL_TAKEN"


REGISTER_MUTATION = """
mutation SeedRegister($input: RegisterInput!) {
  register(input: $input) {
    user { id email }
    errors { field code message }
  }
}
"""

LOGIN_MUTATION = """
mutation SeedLogin($input: LoginInput!) {
  login(input: $input) {
    user { id email }
    errors { field code message }
  }
}
"""

EXISTING_ISSUES_QUERY = """
query SeedExistingIssues {
  issues(first: 1) { nodes { id identifier } }
}
"""

TEAMS_QUERY = """
query SeedTeams($slug: String!) {
  teams(workspaceSlug: $slug) {
    id
    key
    workflowStates { id category }
  }
}
"""

ISSUE_CREATE_MUTATION = """
mutation SeedIssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    issue { id identifier teamId workflowStateId }
    errors { field code message }
  }
}
"""

ISSUE_UPDATE_MUTATION = """
mutation SeedIssueMove($id: UUID!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    issue { id identifier workflowStateId }
    errors { field code message }
  }
}
"""


# The demo backlog: a spread across all five workflow categories and all five
# priorities, so every badge, filter and empty-state in the UI has something
# to render. `priority` follows the frontend's convention (0 unset, 1 urgent,
# 4 low) documented in frontend/src/features/issues/lib/priority.ts.
#
# `days` is an offset from today rather than a fixed date, so the seeded board
# does not quietly become a wall of overdue work as the checkout ages.
DEMO_ISSUES = (
    {
        "title": "Session cookie is dropped on Safari 17",
        "description": (
            "Reported twice this week. The cookie is set with SameSite=Lax and "
            "no Secure flag over http://localhost, which Safari 17 appears to "
            "treat differently from Chrome. Reproduce before changing anything."
        ),
        "priority": 1,
        "category": "STARTED",
        "estimate": 3,
        "days": 2,
    },
    {
        "title": "Keyset pagination returns a duplicate row at the page boundary",
        "description": (
            "Two issues created in the same millisecond sort unstably, so the "
            "cursor can point between them. The index is on (created_at, id) -- "
            "the tie-break exists, so this is a cursor-encoding bug."
        ),
        "priority": 1,
        "category": "STARTED",
        "estimate": 5,
        "days": 4,
    },
    {
        "title": "Add a workspace switcher to the sidebar",
        "description": (
            "Blocked on the API: every resolver is still bound to the bootstrap "
            "workspace slug. Needs workspaceSlug as a real argument first."
        ),
        "priority": 3,
        "category": "BACKLOG",
        "estimate": 8,
        "days": 30,
    },
    {
        "title": "Issue list should remember the last filter",
        "description": (
            "Round-trip the filter through the URL rather than local storage, so "
            "a filtered list is a link somebody can send."
        ),
        "priority": 4,
        "category": "BACKLOG",
        "estimate": 2,
        "days": 45,
    },
    {
        "title": "Write the operator runbook for the migration ledger",
        "description": (
            "What --status reports, what a checksum MISMATCH means, and the "
            "reconciliation steps. The runner refuses to repair a ledger on "
            "purpose, so the manual path needs to be written down."
        ),
        "priority": 2,
        "category": "UNSTARTED",
        "estimate": 3,
        "days": 10,
    },
    {
        "title": "Rate-limit the register mutation",
        "description": (
            "Registration discloses that an address is taken -- an accepted "
            "trade, documented in AuthService.register, whose stated mitigation "
            "is rate limiting. This is that mitigation."
        ),
        "priority": 2,
        "category": "UNSTARTED",
        "estimate": 5,
        "days": 14,
    },
    {
        "title": "Empty state for a team with no issues",
        "description": "Shipped. Copy reviewed, and the illustration is inline SVG.",
        "priority": 3,
        "category": "COMPLETED",
        "estimate": 1,
        "days": -3,
    },
    {
        "title": "Proxy /graphql through Vite in development",
        "description": (
            "Done. The backend serves no CORS headers, so the browser discarded "
            "every cross-origin response while curl kept working."
        ),
        "priority": 2,
        "category": "COMPLETED",
        "estimate": 2,
        "days": -8,
    },
    {
        "title": "Migrate the issue list to server-side sorting",
        "description": (
            "Canceled: the keyset index already fixes the order, and a sort "
            "argument would need a second index per column to stay fast."
        ),
        "priority": 0,
        "category": "CANCELED",
        "estimate": None,
        "days": None,
    },
)


class SeedError(RuntimeError):
    """A refusal to proceed, safe to show the operator verbatim."""


class GraphqlClient:
    """A GraphQL caller that keeps one session cookie.

    The cookie is captured from whichever response first sets one -- register
    or login -- and replayed on every later request, which is what makes the
    seeded issues carry a `creatorId` instead of being filed anonymously.

    `http.cookiejar` would also do this; a single header is fewer moving parts
    than a jar with a policy, and there is exactly one cookie to carry.
    """

    def __init__(self, url: str):
        self._url = url
        self._cookie: str | None = None

    def execute(self, document: str, variables: dict) -> dict:
        body = json.dumps({"query": document, "variables": variables}).encode("utf-8")

        request = urllib.request.Request(self._url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")

        if self._cookie is not None:
            request.add_header("Cookie", self._cookie)

        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                payload = json.load(response)
                self._remember_cookie(response.headers.get("Set-Cookie"))
        except urllib.error.URLError as error:
            raise SeedError(
                f"Could not reach the GraphQL API at {self._url}: {error.reason}"
            ) from error

        # A GraphQL error here is a fault rather than a rejected input --
        # rejected input arrives in a payload's `errors` list instead -- so it
        # stops the seed rather than being folded into a result.
        if payload.get("errors"):
            raise SeedError(
                "The API reported an error: "
                + "; ".join(item.get("message", "?") for item in payload["errors"])
            )

        return payload["data"]

    def _remember_cookie(self, header: str | None) -> None:
        if not header:
            return

        # Everything before the first ';' is `name=value`; the rest is Path,
        # Max-Age, HttpOnly and SameSite, which are the browser's business.
        self._cookie = header.split(";", 1)[0]


def _issue_messages(errors: list[dict]) -> str:
    return "; ".join(f"{item['field']}: {item['message']}" for item in errors)


def sign_in(client: GraphqlClient) -> str:
    """Register the demo account, or log into it if a previous run made it.

    Returns a one-line description of which of the two happened, so the
    launcher's output says whether this run created the account.
    """
    data = client.execute(
        REGISTER_MUTATION,
        {
            "input": {
                "email": DEMO_EMAIL,
                "password": DEMO_PASSWORD,
                "name": DEMO_NAME,
            }
        },
    )
    payload = data["register"]

    if payload["user"] is not None:
        return f"Registered {DEMO_EMAIL}."

    codes = {error["code"] for error in payload["errors"]}

    if EMAIL_TAKEN not in codes:
        raise SeedError(
            "Could not register the demo account: " + _issue_messages(payload["errors"])
        )

    # Already registered by an earlier run. Log in instead, so the rest of the
    # seed still runs with a session and files issues under a real creator.
    data = client.execute(
        LOGIN_MUTATION,
        {"input": {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}},
    )
    payload = data["login"]

    if payload["user"] is None:
        raise SeedError(
            f"{DEMO_EMAIL} is already registered, but the demo password no "
            "longer works for it. Rebuild the local database with: "
            ".\\run-vector-local.ps1 -Fresh"
        )

    return f"Signed in as {DEMO_EMAIL} (the account already existed)."


def workflow_states_by_team(client: GraphqlClient) -> dict[str, dict[str, str]]:
    """team id -> workflow state category -> workflow state id.

    Keyed by category rather than by name, because the name belongs to the
    team and may be renamed freely -- `migrations/005_team_workflows.sql` says
    so explicitly, and the GraphQL enum exists so clients can branch on the
    category instead.
    """
    data = client.execute(TEAMS_QUERY, {"slug": WORKSPACE_SLUG})

    return {
        team["id"]: {state["category"]: state["id"] for state in team["workflowStates"]}
        for team in data["teams"]
    }


def create_issue(client: GraphqlClient, spec: dict) -> dict:
    """File one demo issue, and return the created issue."""
    due_date = None

    if spec["days"] is not None:
        due_date = (date.today() + timedelta(days=spec["days"])).isoformat()

    data = client.execute(
        ISSUE_CREATE_MUTATION,
        {
            "input": {
                "title": spec["title"],
                "description": spec["description"],
                "priority": spec["priority"],
                "estimate": spec["estimate"],
                "dueDate": due_date,
            }
        },
    )
    payload = data["issueCreate"]

    if payload["issue"] is None:
        raise SeedError(
            f"Could not create {spec['title']!r}: " + _issue_messages(payload["errors"])
        )

    return payload["issue"]


def move_issue(client: GraphqlClient, issue_id: str, state_id: str) -> None:
    """Put an issue in a workflow state other than the one it was filed into.

    A separate mutation rather than a field on the create, because that is the
    only shape the API offers: `IssueCreateInput` has no `workflowStateId`, on
    the stated grounds that filing straight into another state is a move.
    """
    data = client.execute(
        ISSUE_UPDATE_MUTATION,
        {"id": issue_id, "input": {"workflowStateId": state_id}},
    )
    payload = data["issueUpdate"]

    if payload["issue"] is None:
        raise SeedError(
            "Could not move a seeded issue into its workflow state: "
            + _issue_messages(payload["errors"])
        )


def seed_issues(client: GraphqlClient) -> int:
    """File every demo issue and put it in its intended state.

    The states are looked up per team from the created issue's own `teamId`,
    not from "the first team in the workspace". `issueCreate` files against
    whichever team the server considers the default, and reading it back is
    the only way to be sure the state we then move the issue into belongs to
    the same team -- a state from another team is a foreign key the update
    would be rejected for.
    """
    states = workflow_states_by_team(client)
    created = 0

    for spec in DEMO_ISSUES:
        issue = create_issue(client, spec)
        created += 1

        target = states.get(issue["teamId"], {}).get(spec["category"])

        if target is None:
            raise SeedError(
                f"Team {issue['teamId']} has no {spec['category']} workflow "
                "state, so the demo board cannot be built. The local database "
                "is missing what migration 005 seeds."
            )

        # Already where it belongs: `issueCreate` files into the team's
        # unstarted state, so an UNSTARTED issue needs no move at all.
        if target != issue["workflowStateId"]:
            move_issue(client, issue["id"], target)

        print(f"    {issue['identifier']}  {spec['title']}")

    return created


def assert_loopback(url: str) -> None:
    """Refuse any endpoint that is not on this machine.

    The guard is the reason it is safe for this file to publish a password.
    Seeding is a write path, and a seeder that can be pointed at a hostname
    is one typo away from writing demo issues into somebody's real workspace.
    """
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise SeedError(f"--api-url must be an http(s) URL, not {url!r}")

    if parsed.hostname not in LOOPBACK_HOSTS:
        raise SeedError(
            f"Refusing to seed {parsed.hostname!r}: this script writes a demo "
            "account with a published password and only ever runs against a "
            "local database. Allowed hosts: " + ", ".join(sorted(LOOPBACK_HOSTS))
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Seed the local demo workspace through the GraphQL API.",
    )
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:8000/graphql",
        help="GraphQL endpoint. Must be on loopback. (default: %(default)s)",
    )
    arguments = parser.parse_args(argv)

    assert_loopback(arguments.api_url)

    client = GraphqlClient(arguments.api_url)

    # Asked before anything is written, and the whole of this script's
    # idempotency. An issue already present means a previous run seeded this
    # database (or the owner has been using it), and either way filing nine
    # more would be the wrong answer.
    existing = client.execute(EXISTING_ISSUES_QUERY, {})["issues"]["nodes"]

    if existing:
        print(f"    Workspace already has issues ({existing[0]['identifier']}).")
        print("    Nothing seeded.")
        _print_credentials()
        return 0

    print(f"    {sign_in(client)}")

    created = seed_issues(client)

    print(f"    Seeded {created} issues across 5 states and 5 priorities.")
    _print_credentials()

    return 0


def _print_credentials() -> None:
    """Print the demo sign-in.

    Printed on every run, including the run that seeds nothing: the owner who
    needs these is as likely to be on their second launch as their first.
    """
    print("")
    print("    Demo sign-in -- local throwaway database, no real data:")
    print(f"      email     {DEMO_EMAIL}")
    print(f"      password  {DEMO_PASSWORD}")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SeedError as error:
        raise SystemExit(f"error: {error}") from error
