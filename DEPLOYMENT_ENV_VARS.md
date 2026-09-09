# Deployment configuration — variable NAMES only

**No value in this file is a real one.** Every name below is read from
`app/config.py`, `app/rest/github.py`, `app/rest/slack.py` and
`frontend/src/lib/config/env.ts`; nothing here is invented, and nothing here
is a credential. Real values are typed into the Render dashboard and nowhere
else. `.env` is gitignored, `.env.example` holds placeholders, and
`tests/test_phase1a2_gates.py::test_no_tracked_file_assigns_a_provider_secret`
fails if a real one is ever pasted into a tracked file.

---

## This is a staging / demo deployment

It exists so there is a real public URL to click through. It is not a
production service: no uptime is claimed, the free Render instance sleeps
after fifteen minutes of no traffic and takes tens of seconds to wake, the
free Neon compute suspends on its own, there are no backups, and known
defects are expected to be present.

**`ENVIRONMENT` is still set to `production`, and that is correct.** The two
words describe different things:

| | |
|---|---|
| `ENVIRONMENT=production` | the **runtime mode**. The only value under which `app/graphql/router.py` serves no GraphiQL and the schema answers no introspection, and the only value under which `app/http_cookies.py` marks the session and OAuth-state cookies `Secure`. |
| "staging" | what the deployment is **for**. A label for humans. |

Do not "fix" this by setting `ENVIRONMENT=staging`: `app/config.py` accepts
only `development`, `test` and `production`, so the service would fail to
start — and `development`, which does start, would put GraphiQL,
introspection and non-`Secure` cookies on a public host.

---

## REQUIRED

Without these the service does not start. `app/config.py` gives neither a
default, deliberately, so a deployment that forgets one crashes loudly
instead of running with a protection silently off.

| Name | What it is |
|---|---|
| `DATABASE_URL` | The Neon PostgreSQL DSN. Secret — it carries a password. See **Neon** below for the TLS parameters it must end with. |
| `ENVIRONMENT` | `production`. See above. |

## OPTIONAL — set for this deployment

| Name | What it is |
|---|---|
| `PUBLIC_BASE_URL` | This deployment's origin, `https://<render-host>`, with no trailing path. Used for exactly one thing: the deep link in a Slack message, which is built by a background loop that is serving no request and so has no `Host` header to infer an origin from. Unset means messages go out **without** a link rather than with a guessed one. Not knowable until Render has assigned the host, so it is filled in after the first deploy. |
| `EMBEDDING_WORKER_ENABLED` | `true` on this deployment, and already committed in `render.yaml` because it is a decision rather than a credential. Read **The 512 MB verdict** before changing it. Must be `true` or `false` — never blank, which pydantic rejects and which would stop the boot. |
| `FORWARDED_ALLOW_IPS` | `*`, committed in `render.yaml`. Read by **uvicorn**, not by `app/config.py`. Render terminates TLS at its edge and forwards plain HTTP with `X-Forwarded-Proto: https`; uvicorn believes that header only from a peer in this list, which defaults to `127.0.0.1`. Without it `request.url.scheme` stays `http` and **no `Strict-Transport-Security` header is sent at all**. `*` is safe only because the container publishes no port and is reachable solely through Render's proxy. |

## OPTIONAL — GitHub App

All five or none. Every one defaults to `None`, and `None` is what makes the
deployment report itself `UNCONFIGURED` — which is a different answer from
`DISCONNECTED` and has to stay different. **Never set one of these to an
empty string**: `""` is not `None`, so `GithubAppConfig.configured` would call
a credential-less deployment configured and put a Connect button in front of
a user whose every click dead-ends. This is why `render.yaml` does not list
them at all: a `sync: false` entry left blank in the dashboard becomes `""`.

| Name | Secret? | What it is |
|---|---|---|
| `GITHUB_APP_ID` | no | The App's numeric id. A public fact about the App. |
| `GITHUB_CLIENT_ID` | no | The App's OAuth client id; it travels in the authorize URL. |
| `GITHUB_CLIENT_SECRET` | **yes** | Exchanges an OAuth code for a token. |
| `GITHUB_WEBHOOK_SECRET` | **yes** | The only thing distinguishing a real delivery from a stranger POSTing JSON. |
| `GITHUB_APP_PRIVATE_KEY` | **yes** | The App's RSA private key, PEM, whole — newlines and all. The most valuable secret this process holds. |
| `GITHUB_PRIVATE_KEY_PATH` | path | The same key read from a file instead. **Exactly one** of this and `GITHUB_APP_PRIVATE_KEY` may be set. There is no filesystem to put a file on in this deployment, so use `GITHUB_APP_PRIVATE_KEY`. |
| `GITHUB_OAUTH_CALLBACK_URL` | no | Where GitHub sends the browser back. A copy of a value GitHub already holds — OAuth fails if the two disagree. |
| `GITHUB_REDIRECT_ALLOWLIST` | no | Where the install callback may send the browser **next**, as comma-separated origins. Empty by default, which means "redirect nowhere" and is the safe failure. See **Callback URLs** below — this is the one variable that must hold production *and* development at once. |

## OPTIONAL — Slack

All three of the credentials or none; `slack_configured` is all-or-nothing for
the same reason.

| Name | Secret? | What it is |
|---|---|---|
| `SLACK_CLIENT_ID` | no | Public by design; it travels in the authorize URL. |
| `SLACK_CLIENT_SECRET` | **yes** | Exchanges an OAuth code for a bot token. |
| `SLACK_SIGNING_SECRET` | **yes** | The only thing distinguishing a real Slack delivery from anyone on the internet posting to the events endpoint. |
| `SLACK_OAUTH_CALLBACK_URL` | no | Registered provider-side, and sent as `redirect_uri` on **both** legs — Slack requires the two to match byte for byte. |

## DEVELOPMENT-ONLY — never set on Render

| Name | Why not |
|---|---|
| `VITE_GRAPHQL_URL` | A **build-time** variable, inlined into the JavaScript bundle by Vite. Its default is the relative path `/graphql`, which is already correct here because the page and the API share an origin. Setting it to an absolute URL would make the browser discard every response, because this backend installs no CORS middleware. Nothing in the Dockerfile passes it, and nothing should. |
| `VECTOR_API_PORT`, `VECTOR_WEB_PORT` | `docker-compose.yml` host-port overrides. Meaningless outside a local compose stack. |
| `PORT` | Set by Render itself. The image binds `0.0.0.0:${PORT:-8000}`; do not override it. |

---

## Neon

### Already provisioned — this deployment reuses an existing database

The staging service points at the Neon database this project has been using
all along (`neondb`, PostgreSQL **18.6**), not a new project. Its state was
verified before anything was applied to it, and the schema is now current:

* it held **one** table, `issues` — migration 001's, matching that file's
  columns, primary key, `issues_priority_range` check and keyset index — and
  nothing belonging to any other project: one schema (`public`), no foreign
  tables, no extensions beyond `plpgsql`;
* it had **no `schema_migrations` ledger**, because 001 was applied by hand
  before the runner existed. `--status` adopted 001 after fingerprinting the
  live table against the file, which is the one path that writes that row
  without taking the claim on trust;
* migrations **002 through 032 were then applied forward, in order**, each
  through `python -m scripts.apply_migration`. `--status` now reports 32
  applied, every checksum `ok`, `Pending: none`.

**Nothing was dropped, reset or recreated.** Across all 32 migrations there is
no `DROP TABLE`, no `TRUNCATE`, no `DELETE FROM` and no `DROP COLUMN` — the
only `DROP` is `002`'s of `issues_created_at_id_idx`, replaced two statements
later by the workspace-leading index that supersedes it. The two rows that
were in `issues` are still there; `002` backfilled them into the bootstrap
tenant it creates (workspace `vector`, team `Core`), which is exactly what
that migration was written to do.

**So the first container boot is a no-op.** The image's CMD applies every
`migrations/*.sql` before exec'ing uvicorn; with the ledger complete the
runner skips all 32 and starts the server.

**PostgreSQL 18 is mandatory, not preferred.** `migrations/001_issues.sql`
defaults its primary keys to `uuidv7()`, which is native to 18 and does not
exist in 17. A project created on 17 fails on the very first migration.
Neon has had 18 generally available since May 2026 and defaults new projects
to it — but confirm the version on the project's dashboard rather than
assuming it.

**Extensions: nothing to enable by hand.** The migrations create all three
themselves, each with `IF NOT EXISTS`:

| Extension | Created by |
|---|---|
| `btree_gist` | `migrations/008_cycles.sql` |
| `btree_gin` | `migrations/011_search.sql` |
| `vector` (pgvector) | `migrations/025_semantic_search.sql` |

pgvector is available on every Neon plan including free, at no extra cost,
and is enabled **per database** with `CREATE EXTENSION vector` — which is
exactly what migration 025 runs, as the role Neon gives you. So there is no
console step for it. (No HNSW or IVFFlat index is built: 025 creates a plain
btree, so Neon's index dimension limits do not apply.)

### TLS — and the classic asyncpg failure

Neon refuses unencrypted connections, and **asyncpg is not psycopg**. What is
true of asyncpg 0.31 (the pinned version), verified against its
`connect_utils.py` rather than assumed:

* `sslmode` **in the URL query string is honoured** — `connect_utils.py` pops
  it out of the query and maps it onto its own `SSLMode`. So this is not the
  usual "asyncpg ignores libpq parameters" trap.
* With **no** `sslmode` at all, asyncpg defaults to **`prefer`** for a TCP
  address: it tries TLS, does not verify the certificate, and silently falls
  back to plaintext if the server refuses. Neon would not accept the
  fallback, so the connection is encrypted in practice — but "encrypted
  because the other end insisted" is not a posture to deploy on.
* `sslmode=require` gives TLS that cannot be downgraded, but with
  `verify_mode = CERT_NONE`: encrypted, **not authenticated**.
* `sslmode=verify-full` **fails outright** unless a CA file is named. asyncpg
  has no `sslrootcert=system` (that is a libpq 16 feature it has not
  adopted); with no `~/.postgresql/root.crt` it raises
  `ClientConfigurationError` before opening a socket. This is the failure
  worth knowing about in advance, because it looks like a broken DSN.

So the DSN ends with **both** parameters, and the CA file is the one Debian
already ships in the image:

```
?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt
```

Neon's certificate is signed by a public CA, so that path is all the trust
store needed and the hostname is genuinely checked. Neon's copy-paste
connection string ends in `?sslmode=require`; replace that suffix.

`?channel_binding=require` may also appear in what Neon hands you. **Remove
it — but not for the reason previously written here.** An earlier draft of
this file said asyncpg rejects it and the connection fails. That was wrong,
and it was corrected by connecting rather than by re-reading the changelog:
the real `.env` DSN carries `sslmode=require&channel_binding=require` and
connects fine — all 31 forward migrations were applied to Neon through it.

What actually happens: `connect_utils.py` pops the parameters it knows and
sweeps whatever is left into `server_settings`, which are sent as startup
parameters. Neon's proxy strips `channel_binding` there, so it never reaches
PostgreSQL — `SELECT current_setting('channel_binding')` raises
`UndefinedObjectError` on a connection that carried it.

So it is inert on Neon, not fatal. Remove it anyway, because inert is the
problem: asyncpg applies no channel-binding requirement of any kind, so the
parameter reads like a security control while enforcing nothing, and on a
PostgreSQL endpoint without Neon's proxy in front the same sweep would send
an unrecognised startup parameter to the server.

The one claim above that IS verified against the live server: `verify-full`
with no `sslrootcert` really does fail, with
`root certificate file "...\.postgresql\root.crt" does not exist`, before
any socket is opened — and `verify-full` **with** a CA bundle connects.

### Pool sizing

`app/db.py` opens `min_size=1, max_size=5`, and that is already right for
this deployment; it is left alone deliberately. One Render free instance
means one process, so five is the ceiling for the whole service — well inside
any Neon limit, and enough for the request path plus the four background
tasks on the lifespan. Raising it would buy nothing: the instance has 0.1 CPU.

Neon's free compute autosuspends after five minutes of inactivity and drops
its connections when it does. That is handled rather than avoided: asyncpg's
pool checks `is_closed()` on acquire and transparently opens a replacement,
so the first request after a suspend pays the resume latency and nothing
else. It is also why the Render health check is `/healthz` and not `/readyz`
— see below.

---

## The 512 MB verdict on the embedder

**A real sentence-transformer model does not fit, and this deployment does
not pretend otherwise.**

`app/services/embeddings.py::load_embedder` prefers
`sentence-transformers/all-MiniLM-L6-v2` when `sentence_transformers` is
importable and falls back to the in-tree `HashingEmbedder` when it is not.
`sentence-transformers` is **not** in `requirements.txt`, so the deployed
image uses the fallback. The honest arithmetic for the alternative:

* `torch` alone is roughly 800 MB–1 GB installed on Linux CPU, before
  `transformers`, `tokenizers`, `scipy` and `numpy`;
* loading MiniLM-L6-v2 and encoding a batch puts resident memory in the
  400–600 MB range on its own;
* uvicorn, FastAPI, Strawberry and asyncpg are already 120–180 MB before any
  of that.

512 MB is exceeded by the model alone. Adding it would also mean putting
`torch` into a lock that is fully pinned **and hashed**, and downloading
weights at startup on an instance that has no persistent disk — so every cold
start would fetch ~90 MB before serving a request.

**What runs instead, and why the screen stays honest.**
`EMBEDDING_WORKER_ENABLED=true`, so the worker runs with `HashingEmbedder`:
signed feature hashing over tokens and six-character prefixes, L2-normalised
into the same 384 dimensions `vector(384)` is sized for. It costs
approximately nothing in memory and it is a real similarity — it tolerates
word order, length and partial overlap in a way the lexical arm's AND-of-every-term
cannot — but it has seen no corpus and knows no synonyms. "The build keeps
dying" and "CI fails intermittently" are unrelated to it.

The alternative was to leave the worker off. That would be **worse**, not
more honest: the embedder is wired into the GraphQL context unconditionally,
so `embeddingIndexingState.enabled` would still be `true` while the index
stayed permanently empty. `frontend/src/features/semanticSearch/lib/indexState.ts`
would then say "Nothing has been indexed yet — index a batch and search
again" forever, on a deployment where nothing ever would. With the worker on,
every count that screen renders is a true count.

The one sentence that overclaims is `indexState.ts`'s "Every issue in this
workspace can be found by meaning", which is written for a real encoder.
Installing `sentence-transformers` on an instance with more memory changes
the embedder's `name`, which re-queues every vector by itself — no migration,
no backfill, no flag. That is the upgrade path.

---

## Callback URLs — production and local, side by side

The URLs to register provider-side, all on the one origin:

| Provider setting | URL |
|---|---|
| GitHub OAuth callback | `https://<render-host>/integrations/github/oauth/callback` |
| GitHub webhook | `https://<render-host>/integrations/github/webhook` |
| Slack OAuth redirect | `https://<render-host>/integrations/slack/oauth/callback` |
| Slack events request URL | `https://<render-host>/integrations/slack/events` |

`app/main.py` mounts both providers' routers twice — bare and under
`/integrations` — so `/github/oauth/callback` resolves as well, and neither
spelling is a second implementation that can drift. GitHub additionally
answers the older `/github/callback`.

`GITHUB_OAUTH_CALLBACK_URL` and `SLACK_OAUTH_CALLBACK_URL` are single values
copied from what the provider holds, so each deployment has its own; the
Render service gets the Render host and a developer's `.env` keeps whatever
tunnel they use. Nothing needs to be replaced.

**`GITHUB_REDIRECT_ALLOWLIST` is the one that holds both at once.** It is
comma-separated origins, split in exactly one place
(`GithubAppConfig.from_settings`), so adding production does not remove
development:

```
GITHUB_REDIRECT_ALLOWLIST=<render origin>,<local dev origin>
```

Empty means "redirect nowhere" and the callback answers with a plain page
instead — the safe failure, and the current default.

**On `app/rest/oauth_origin.py`:** it becomes a no-op here, because it only
acts when the start leg's host differs from the callback's, and on a
single-origin deployment they are the same host. That is the intended
outcome and it is **not** evidence the OAuth-state fix works. This topology
removes the condition that triggered the bug rather than exercising the
correction; proving the fix still requires a split-origin setup, which is
what local development with a tunnel already is.

---

## Security posture, as deployed

Confirmed against the code rather than assumed:

* **Session cookie** (`app/http_cookies.py`): `HttpOnly`, `Path=/`,
  `SameSite=Lax`, and `Secure` because `is_secure_environment` returns true
  exactly when `ENVIRONMENT == "production"`.
* **OAuth state cookies** (`vector_github_state`, `vector_slack_oauth`): the
  same four attributes, from the same helper. State validation is untouched.
* **CORS**: none, and none is needed — one origin. No wildcard, no
  credentialed allowlist.
* **GraphiQL and introspection**: refused, because `ENVIRONMENT=production`.
* **Security headers** (`app/http_headers.py`): set on the application, not
  on a proxy, so they survive with no nginx in front. `Strict-Transport-Security`
  is sent only on a request that arrived over TLS.
* **CSP for the page**: `app/spa.py` sets its own on `index.html`, because the
  middleware's `text/html` policy was written for GraphiQL and would block
  the Google Fonts stylesheet the page links.
* **The image**: no `ENV` carries configuration, and the build context is an
  allowlist that re-excludes `.env`, `.env.*` and `*.pem` inside every
  directory it admits.
* **HSTS**: sent, because `FORWARDED_ALLOW_IPS` is set. See above — without
  it the header is silently absent.

### One property that does NOT carry over — known, and accepted for staging

`app/http_client_ip.py` reads `X-Real-IP` and trusts it, and its own docstring
says exactly why that is safe in the deployments this repository ships:
`frontend/nginx.conf` **overwrites** that header with `$remote_addr`, and the
API is reachable only through nginx, so an outside caller cannot choose the
value.

**There is no nginx here.** Render's proxy adds `X-Forwarded-For` and does not
scrub a client-supplied `X-Real-IP`, so on this deployment a caller can pick
their own IP rate-limit bucket.

What that does and does not cost, from `app/services/auth.py`: the IP budget
is the *loose backstop*, not the primary control. The per-email-address budget
and the argon2 semaphore in `app/services/passwords.py` are unaffected and are
what actually bound a credential-stuffing attempt. So this is a weakened
backstop on a staging deployment, not an open login endpoint.

The fix is the one already recorded as a `ponytail:` note in that module — a
trusted-proxy setting, so `X-Real-IP` is read only when a proxy is known to
set it — and it is deliberately not made here, because that function is shared
with the compose and Kubernetes stacks where the header *is* trustworthy and
changing it blind would degrade them.

---

## Health and readiness

| Path | Touches PostgreSQL | Used by |
|---|---|---|
| `/healthz` | no | **Render's health check.** |
| `/readyz` | yes | `docker-compose.yml`, `k8s/30-api.yaml`, humans. |

Render restarts a service that fails its health check. Pointing that at
`/readyz` would turn a Neon compute resuming from autosuspend into a restart
of an application that was working perfectly — and restarts do not fix
databases. It would also poll the database forever, which on a free Neon
project means the compute never suspends at all.
