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

## Run the deployment shape locally (Docker Compose)

The launcher above is the *development* loop: hot reload, a seeded demo
workspace, a browser. This is the other one — the same images CI builds and
`k8s/` deploys, on one machine:

```bash
docker compose up --build
# then http://localhost:5173
```

PostgreSQL 18 with pgvector → every migration applied through
`scripts/apply_migration` → FastAPI → nginx serving the built bundle with the
API proxied onto the same origin. `docker compose down` stops it and keeps the
database; `docker compose down -v` throws the database away too.

| | |
| --- | --- |
| `http://localhost:5173` | The application. GraphiQL is at `/graphql`. |
| `127.0.0.1:8000` | The API directly, for `curl`. |
| `127.0.0.1:5433` | PostgreSQL, for `psql`. User, password and database are all `vector`. |
| `VECTOR_API_PORT=8001 docker compose up` | If 8000 or 5173 is already taken — `run-vector-local.ps1` uses both. `VECTOR_WEB_PORT` is the other one. |

Nothing here reads `.env`, and no value in `docker-compose.yml` is a
credential: the database password is the literal `vector`, in a container the
file creates and destroys, published on loopback only.

Two background tasks run inside every API container — the Slack delivery loop
(always) and the embedding worker (`EMBEDDING_WORKER_ENABLED`, on in this
stack). Both claim work with `FOR UPDATE SKIP LOCKED`, so
`docker compose up --scale api=3` divides the backlog rather than duplicating
it. Drop the `api` port mapping first; three containers cannot each own 8000.

## Deploy to Kubernetes

`k8s/` holds plain manifests — no Helm, no kustomize — for kind, k3d, minikube
or a real cluster. Build and side-load the images, supply the one Secret, and
apply:

```bash
docker build -t vector-api:local .
docker build -t vector-web:local frontend

minikube image load vector-api:local        # kind: kind load docker-image ...
minikube image load vector-web:local

kubectl apply -f k8s/00-namespace.yaml
kubectl create secret generic vector-secrets -n vector \
  --from-literal=POSTGRES_PASSWORD='<choose one>' \
  --from-literal=DATABASE_URL='postgresql://vector:<the same one>@postgres:5432/vector'

kubectl apply -f k8s/
kubectl rollout status -n vector deployment/api
```

The Secret is created by hand and is in no file here: every manifest
references it by name and key and never carries a value.

On Windows 11, `minikube image load` fails with `exec: "wmic": executable file
not found in %PATH%` — the tool was removed from the OS and minikube 1.35 still
shells out to it. Pipe the images in instead, which is what that command does
anyway:

```bash
docker save vector-api:local vector-web:local | docker exec -i minikube docker load
docker save pgvector/pgvector:pg18 | docker exec -i minikube docker load
```

Migrations are applied from this repository, against the cluster's database,
because the runtime image deliberately ships `app/` and nothing else —
`migrations/` and `scripts/` are outside its build context by design, and
`tests/test_phase1a2_gates.py` fails if they are added to it:

```bash
kubectl port-forward -n vector svc/postgres 15432:5432 &
export ENVIRONMENT=development
export DATABASE_URL='postgresql://vector:<the same one>@127.0.0.1:15432/vector'
for f in migrations/*.sql; do python -m scripts.apply_migration "$f"; done
python -m scripts.apply_migration --status     # expect "Pending: none"
```

Then reach it with `minikube service web -n vector --url`, or
`kubectl port-forward -n vector svc/web 5173:80`.

`k8s/20-postgres.yaml` is for development clusters only — one replica, one
PVC, no replication and no backups. A production cluster does not apply it and
points `DATABASE_URL` at a managed PostgreSQL instead; nothing else in the
directory changes, because the API reads a Secret key rather than a hostname.

`k8s/10-configmap.yaml` sets `ENVIRONMENT: production`, which marks the
session cookie `Secure`. Over plain http — a port-forward, a NodePort — the
browser will not store it, so sign-in appears to work and the next request is
anonymous. Put TLS in front of it, or set `development` on a throwaway
cluster.

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
| `EMBEDDING_WORKER_ENABLED` | `true` \| `false` (default `false`) | Whether this process also drains `embedding_jobs` for semantic search. Safe on every replica: the queue is claimed with `FOR UPDATE SKIP LOCKED`. |
| `PUBLIC_BASE_URL` | An origin, or unset | Where Slack deep links point. The delivery loop runs on no request, so it has no Host header to infer one from; unset means messages go out without a link rather than with a guessed one. |

The first two are required and neither has a default. `ENVIRONMENT`
especially: a default would have to be *some* environment, and any deployment
that forgot to set it would quietly get that one's behaviour.

The GitHub and Slack integrations add optional variables of their own, all of
them credentials; `app/config.py` documents each one and treats "none set" as
UNCONFIGURED rather than broken.

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
