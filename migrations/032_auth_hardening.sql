-- Rate limiting for the unauthenticated auth surface: one counter table, and
-- the argument for why it is a table at all.
--
-- app/services/auth.py has documented its own enumeration channel since the
-- day it was written. `register` discloses that an address is taken -- that is
-- deliberate, and the docstring says so -- and then names the mitigation:
--
--     "The mitigations for it are rate limiting and eventually address
--      verification, neither of which is this phase."
--
-- The mitigation was never built. So today an unauthenticated client reads the
-- entire users table out of `register`, one EMAIL_TAKEN at a time, at whatever
-- rate the network allows -- and guesses passwords against `log_in` at the same
-- rate, because nothing counts anything anywhere. This file is that counter.
--
-- It also closes a second hole the same counter happens to close.
-- `Argon2PasswordHasher` runs every hash on `asyncio.to_thread`, which is the
-- event loop's DEFAULT executor -- min(32, cpus + 4) threads -- and every
-- concurrent argon2id operation allocates 64 MiB. k8s/30-api.yaml sets
-- `limits.memory: 512Mi`. Eight concurrent log-ins to an address that does not
-- exist are therefore an OOMKill, with no account and no valid password
-- required, because the decoy-hash path spends the full cost for an address
-- that matches nothing. That path is correct and stays; what stops the
-- amplifier is a bound on how many attempts get that far, which is this table,
-- backed by a semaphore in app/services/passwords.py for the burst that arrives
-- inside one window.
--
--
-- WHY POSTGRESQL AND NOT A DICT IN THE PROCESS
--
-- k8s/30-api.yaml says `replicas: 2`. An in-process counter is per replica, so
-- a client spraying the Service gets 2x the budget and gets the whole budget
-- back on every rollout, every OOMKill and every node drain -- which is to say
-- the limit would be loosest at exactly the moments something is already wrong.
-- A limit that resets when the process does is not a limit; it is a log line
-- with an if-statement.
--
-- The alternative shared store is Redis, and it is rejected on cost rather than
-- on fit: it fits well. It is a new dependency, a new container in
-- docker-compose.yml, a new Deployment and Service in k8s/, a new failure mode
-- on the log-in path, and a new question about what happens when it is down --
-- all bought to add three integers a minute to a system that is already running
-- a transactional database every one of these requests already talks to.
-- PostgreSQL is the shared state this application HAS. An upsert on a
-- two-column primary key is a row lock and an index probe, which is cheaper
-- than the argon2 hash it is standing in front of by three orders of magnitude.
--
--
-- WHY A FIXED WINDOW AND NOT A SLIDING ONE
--
-- One row per (scope, subject) holding a count and the instant its window
-- opened. The count rolls over to 1 when the window has aged out; there is no
-- separate expiry pass on the read path, so a bucket nobody touches costs
-- nothing until the sweep collects it.
--
-- The known flaw of a fixed window is that a client can spend its whole budget
-- at the end of one window and again at the start of the next -- 2x the
-- nominal rate across the boundary. That is priced in below: the budgets in
-- app/services/auth.py are chosen so that twice them is still refused long
-- before anything expensive has happened at scale.
--
-- The sliding alternative is a row per ATTEMPT and a COUNT over the last N
-- minutes. It is exact, and it trades this table's bounded size -- one row per
-- active subject, ever -- for one that grows with traffic and needs a sweep of
-- its own with a much shorter deadline. Exactness is not what a rate limit is
-- for. It is a blunt instrument by design, and the blunt version is the one
-- whose worst case is a row per IP.
--
--
-- WHAT A SUBJECT IS, AND WHY ONE OF THEM IS A DIGEST
--
-- `scope` names the operation AND the kind of key together -- 'login:ip',
-- 'login:email', 'register:ip' -- so that one operation's budget can never be
-- spent by another's, and so that adding a fourth is a new string rather than a
-- new column. `subject` is the value: an IP address, or the SHA-256 hex digest
-- of a normalised email address.
--
-- The address is hashed and the IP is not, and that asymmetry is deliberate.
-- Storing addresses here would build a second copy of the user list -- plus
-- every address anyone ever GUESSED at -- in a table with no tenant, no
-- foreign key and no reason for anybody to have thought about who can read it.
-- The limiter only ever needs equality, which a digest gives. An IP is left
-- readable because it is already in every access log the deployment keeps, and
-- because "which address is being blocked" is a question nobody can answer at
-- 3am from a column of digests. See app/services/auth.py for the one function
-- that builds a subject.
--
-- No `user_id`, and no foreign key to `users`. A row here is written for
-- addresses that have no account -- that is most of them, during an
-- enumeration sweep -- so a reference would be unsatisfiable for exactly the
-- traffic this table exists to count. It also means the limiter's writes never
-- touch `users`, so a burst of guesses cannot contend with a real sign-up.
--
--
-- WHAT THIS FILE DOES NOT CREATE
--
-- An index for the session sweep. migrations/003_auth.sql already created
-- `sessions_expires_at_idx`, and said in its own comment that it was creating
-- it for a sweep that did not exist yet:
--
--     "Logout deletes a row; nothing else ever does [...] and a sweep is not a
--      maybe."
--
-- It was right. app/services/auth.py now has the sweep, and the index it
-- needs has been sitting there since 003 waiting for it. Adding a second one
-- would be a duplicate write cost on every session issued.
--
-- Nothing for invitation acceptance either, which the audit that produced this
-- file asked about. `invitationAccept` resolves `viewer_user_id` before it
-- touches the service, so it is not reachable unauthenticated; its token is 32
-- bytes from `secrets.token_urlsafe`, so guessing it is not a thing a budget
-- makes harder; and the failure it returns is already one answer for four
-- situations by design. A counter there would cost every caller of
-- `MembershipService` a new constructor argument to defend against a search
-- of 2^256.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/032_auth_hardening.sql`.


CREATE TABLE auth_rate_limits (
    -- The operation and the key kind, together: 'login:ip', 'login:email',
    -- 'register:ip'. Free-form TEXT rather than an enum, because the values
    -- are chosen by app/services/auth.py and an enum would mean a migration
    -- every time a fourth operation wanted a budget -- for a column no query
    -- ever filters on by anything but equality to a constant the application
    -- supplied.
    scope TEXT NOT NULL,

    -- An IP address, or the SHA-256 hex digest of a normalised email. Never a
    -- raw address; see the header.
    --
    -- 128 covers both with room: an IPv6 address with a zone is at most 45
    -- characters and a hex digest is exactly 64. The ceiling is here so that a
    -- caller that ever passed something unbounded -- a header, a user agent,
    -- a whole request body -- fails at the database rather than growing rows
    -- of arbitrary size in the one table that is written by unauthenticated
    -- traffic.
    subject TEXT NOT NULL,

    -- When the window this count belongs to opened. Set from now() by the
    -- upsert, on the database's clock and never the application's, for the
    -- reason migrations/003_auth.sql gives for `sessions.expires_at`: two
    -- replicas with drifting clocks must not disagree about when a window
    -- rolls, and a window instant computed in Python is a limit whose real
    -- length depends on which pod served the request.
    window_started_at TIMESTAMPTZ NOT NULL,

    -- Attempts inside that window, counting from 1. There is no row for zero
    -- attempts, so the CHECK below is the invariant rather than a guess: a row
    -- exists because something was counted.
    --
    -- INTEGER and not BIGINT. The count is compared against a budget in the
    -- tens and the row is reset every window; a subject that reached 2^31
    -- would have had to spend two billion requests inside fifteen minutes
    -- without anything else in the deployment noticing.
    attempts INTEGER NOT NULL,

    -- The whole access pattern, and the reason there is no surrogate id. Every
    -- statement against this table addresses exactly one bucket by both
    -- columns, so the primary key IS the index -- and it is what makes the
    -- upsert atomic under two replicas: concurrent increments for one subject
    -- serialise on this row's lock rather than interleaving into a lost update.
    PRIMARY KEY (scope, subject),

    CONSTRAINT auth_rate_limits_attempts_positive
        CHECK (attempts > 0),

    -- Both halves of the key are supplied by the application, so these guard a
    -- category of bug rather than bad input: a caller that ever passed an
    -- empty subject would collapse every anonymous request into one shared
    -- bucket and lock out the entire deployment, and it would do it quietly.
    CONSTRAINT auth_rate_limits_scope_shape
        CHECK (length(scope) BETWEEN 1 AND 64),

    CONSTRAINT auth_rate_limits_subject_shape
        CHECK (length(subject) BETWEEN 1 AND 128)
);


-- For the sweep, which deletes buckets whose window has aged out.
--
-- The table is bounded by the number of distinct subjects, not by traffic, so
-- it does not grow the way `sessions` does -- but "one row per IP that ever
-- attempted a log-in" is still unbounded over a long enough run against an
-- IPv6 internet, and a table nothing ever deletes from is the defect this
-- migration's own sweep exists to fix. Collecting stale buckets on the same
-- pass costs one more statement and keeps this table's size a function of who
-- is active rather than of who ever was.
CREATE INDEX auth_rate_limits_window_idx ON auth_rate_limits (window_started_at);
