# Vector

A Linear-style issue tracker, built as a backend learning project.

## Setup

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
| `ENVIRONMENT` | `development` \| `test` \| `production` | Gates GraphiQL and schema introspection. |

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
