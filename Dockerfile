# syntax=docker/dockerfile:1

# Vector runtime image.
#
# Two stages so that pip, its cache and the build-time metadata stay in the
# builder and never reach the shipped image: the runtime carries the
# resolved site-packages and the application, nothing else.

# Pinned to a patch release rather than `3.12`, so that rebuilding this
# Dockerfile six months from now does not silently move the interpreter
# underneath a dependency set that was resolved and tested against 3.12.
ARG PYTHON_VERSION=3.12.14


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

# Only the application package. Tests, migrations, schema drafts and
# developer scripts are not part of the running service, and the
# .dockerignore allowlist keeps .env out of the build context entirely.
#
# Left owned by root and merely readable by `vector`: nothing in the image
# should be writable by the process serving requests.
COPY app ./app

USER vector

EXPOSE 8000

# ENVIRONMENT and DATABASE_URL are deliberately NOT given defaults here.
#
# app/config.py makes both mandatory so that a deployment which forgets
# ENVIRONMENT cannot start rather than starting in development mode with
# GraphiQL and introspection served. Baking `ENV ENVIRONMENT=...` into the
# image would hand that decision back to whoever built it and defeat the
# check. Supply both at run time.
#
# app.main:app does not exist -- the module-level application was removed
# so that importing the composition root does not resolve settings as a
# side effect -- so the factory form is required, not stylistic.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
