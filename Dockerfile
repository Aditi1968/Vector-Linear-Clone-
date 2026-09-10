# syntax=docker/dockerfile:1

# Vector runtime image: ONE container that serves the API and the page.
#
# Three stages, and the shipped one carries no compiler, no npm, no pip cache
# and no build metadata -- only the resolved site-packages, the application,
# the migrations, the runner that applies them and the built bundle.
#
# The single-origin arrangement is deliberate and is not a simplification.
# This application authenticates with a database-backed session cookie and
# mints OAuth state cookies, and a cookie goes back only to the host that set
# it; splitting the page and the API across two hosts is precisely the failure
# app/rest/oauth_origin.py exists to correct. See app/spa.py.

# Pinned to a patch release rather than `3.12`, so that rebuilding this
# Dockerfile six months from now does not silently move the interpreter
# underneath a dependency set that was resolved and tested against 3.12.
ARG PYTHON_VERSION=3.12.14

# Pinned to a major rather than a patch, matching frontend/Dockerfile and
# .github/workflows/ci.yml, so the bundle this image ships is built by the
# same Node major that CI gates. A major because the patch stream is where
# Node's security fixes arrive, and a frozen patch is a frozen vulnerability.
ARG NODE_VERSION=22


FROM node:${NODE_VERSION}-alpine AS frontend

WORKDIR /build

# The lockfile and its manifest alone, before any source: the dependency tree
# changes rarely and the source changes every commit, so this keeps editing a
# component from reinstalling 400 packages.
COPY frontend/package.json frontend/package-lock.json ./

# `@playwright/test` is a devDependency and `npm ci` installs devDependencies
# because the build needs vite and tsc. Playwright's install script would
# otherwise download three browsers -- roughly 400 MB -- into a stage whose
# whole job is to run `vite build`, and then throw them away.
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# `npm ci`, never `npm install`. ci installs the lockfile as written and fails
# when package.json and the lockfile disagree; install is allowed to quietly
# rewrite the lockfile to make them agree, which would mean shipping a
# dependency tree no developer and no CI run ever had.
RUN npm ci

COPY frontend/ ./

# `tsc -b && vite build`, both halves -- see package.json. A type error
# therefore fails the IMAGE, which is correct and is not a nuisance: `tsc
# --noEmit` against this repository's references-only tsconfig checks nothing
# and exits 0, and that false green is how a broken build reached main once
# already. See tests/test_phase1a2_gates.py.
RUN npm run build


FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# A virtualenv rather than the system site-packages, purely so the whole
# dependency set is one directory that can be copied to the next stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied on its own, before any source. Source changes far more often than
# the lock does, so keeping them in separate layers means editing a
# resolver does not re-run the install.
COPY requirements.txt ./

# --require-hashes is redundant while requirements.txt carries hashes (pip
# infers it) and is stated anyway: if a future edit drops the hashes, the
# build fails here instead of quietly installing whatever PyPI serves.
#
# Runtime lock only. requirements-dev.txt -- pytest, ruff, mypy, the lock
# tool itself -- is deliberately absent from this image.
RUN pip install --no-cache-dir --require-hashes --requirement requirements.txt


FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# System user: no login shell, no home directory to write into. The
# application neither writes to disk nor mutates its own source at
# startup, so it needs nothing it owns.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin --uid 10001 vector

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv/vector

# Four copies, and three of them are here because a managed platform has no
# bind mounts. docker-compose.yml used to mount `migrations/` and `scripts/`
# from the host precisely because the image did not carry them; nothing can
# mount anything into a Render container, so the schema and the runner that
# applies it have to be inside.
#
# Left owned by root and merely readable by `vector`: nothing in the image
# should be writable by the process serving requests.
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

# The bundle, at `static/` rather than at `frontend/dist/`. app/spa.py gives
# the reason in full: `frontend/dist/` exists on every machine where anyone
# has run `npm run build`, so resolving it there would make `pytest` behave
# differently depending on whether the frontend had been built that day.
COPY --from=frontend /build/dist ./static

USER vector

# Documentation, and the fallback the CMD below defaults to. The port
# actually bound is $PORT when the platform sets one -- Render does -- and
# 8000 when nothing does, which is what docker-compose.yml and
# frontend/nginx.conf's `proxy_pass http://api:8000` expect.
EXPOSE 8000

# ENVIRONMENT and DATABASE_URL are deliberately NOT given defaults here.
#
# app/config.py makes both mandatory so that a deployment which forgets
# ENVIRONMENT cannot start rather than starting in development mode with
# GraphiQL and introspection served. Baking `ENV ENVIRONMENT=...` into the
# image would hand that decision back to whoever built it and defeat the
# check. Supply both at run time.
#
# MIGRATE, THEN SERVE, AND ONLY THEN. `set -eu` is the load-bearing token in
# the line below: a migration that fails aborts the shell, the container
# exits non-zero, and the platform reports a failed deploy. Without it the
# loop would carry on to the next file and uvicorn would come up against a
# half-applied schema -- an application answering requests over a database
# nobody can describe, which is the one outcome worth crashing to avoid.
#
# No version is named: the set is whatever is in migrations/, in filename
# order, through the repository's own runner so the ledger, the advisory lock
# and the per-file checksum all apply exactly as they do anywhere else.
# Re-running is the normal case and is a no-op -- every restart and every
# redeploy runs this again, and an already-applied version returns "nothing to
# do" rather than re-executing. Two replicas racing are serialised by the
# runner's advisory lock.
#
# ONE PROCESS, not one per file, and that is a start-up fix rather than
# tidying. This was a shell loop invoking the runner once per migration: 32
# interpreter starts. Measured against the real database from a fast machine,
# that loop cost 14.6s, of which 13.8s (432ms x 32) was interpreter start and
# imports and under a second was database work -- nearly all of it "already
# applied, do nothing". The overhead is CPU-bound and a Render free instance
# has 0.1 CPU, so the same work runs into MINUTES there, and uvicorn is not
# started until it finishes: /healthz answers nothing for the whole of it, on
# every boot and every wake from sleep, even when the schema is current.
#
# `--all` is one process, one connection, and the ledger read and checksum
# pass hoisted out of the per-file path (which was quadratic: 32 files x 32
# checksums). Same measurement, same machine: 1.3s. The per-migration
# TRANSACTION is unchanged -- a failure part-way leaves the migrations before
# it committed and the failing one rolled back whole.
#
# `exec` so uvicorn REPLACES the shell rather than being its child: the
# platform's SIGTERM has to reach the server, or every deploy ends in a
# ten-second kill instead of a graceful shutdown.
#
# app.main:app does not exist -- the module-level application was removed
# so that importing the composition root does not resolve settings as a
# side effect -- so the factory form is required, not stylistic.
CMD set -eu; \
    python -m scripts.apply_migration --all; \
    exec uvicorn "app.main:create_app" "--factory" \
        --host 0.0.0.0 --port "${PORT:-8000}"
