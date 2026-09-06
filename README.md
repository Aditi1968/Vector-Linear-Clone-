# Vector

A Linear-style issue tracker, built as a backend learning project.

## Run the whole thing locally

Prerequisites: **Docker Desktop**, **Node.js 20.19+**, **Python 3.12+**, and
nothing else. The launcher creates `.venv`, installs both dependency sets, and
runs its own PostgreSQL 18 container.

```powershell
.\run-vector-local.ps1
```

Database, migrations, backend, frontend, demo data, browser. Ctrl+C stops it.

| Switch | |
| --- | --- |
| `-Fresh` | Rebuild the local database from empty, so 001..N can be seen to apply cleanly. |
| `-Preview` | Serve the production bundle (`npm run build` + `npm run preview`) rather than the dev server. |
| `-NoBrowser` | Start everything, open nothing. |
| `-Stop` | Stop what the launcher started. Only needed if you closed its window instead of interrupting it. |

Local by construction: the launcher never reads `.env`, sets throwaway database
credentials itself, and only ever talks to a container it created, so it cannot
reach Neon. It seeds a demo workspace through the GraphQL API and prints a
throwaway sign-in for it.

## Setup

Only needed to run against a database of your own rather than the launcher's.

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # then edit DATABASE_URL
export $(grep -v '^#' .env | xargs)
```

## Configuration

| Variable | Values | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL DSN | The database the pool connects to. |
| `ENVIRONMENT` | `development` \| `test` \| `production` | Gates GraphiQL, schema introspection, and the session cookie's `Secure` flag. |

Both are required and neither has a default. `ENVIRONMENT` especially: a
default would have to be *some* environment, and any deployment that forgot
to set it would quietly get that one's behaviour.

## Running

```bash
uvicorn app.main:create_app --factory --reload
```

`--factory` is not optional. `app.main` exposes a `create_app()` factory and
no module-level application, so that importing the module neither resolves
settings nor builds an app.

## Tests

```bash
python -m pytest -q              # default suite; no database required
python -m pytest -q -m db        # PostgreSQL suite; requires Docker
```
