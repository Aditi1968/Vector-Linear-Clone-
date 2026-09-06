-- Authentication: user accounts, and the server-side sessions that stand in
-- for a password once one has been checked.
--
-- Two tables, one file, because they are one guarantee: a session is only
-- meaningful as a reference to a user, and a users table with no session
-- store means the password is re-sent and re-verified on every request.
--
-- What this migration deliberately does NOT create is any link between a user
-- and a workspace. Authentication answers "who is this", and nothing else.
-- Membership -- which workspaces a user may act in, and in what role -- is a
-- separate table owned by a separate migration, and keeping it out of here is
-- what stops "authenticated" and "authorised" from becoming the same column.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/003_auth.sql`, for the reasons spelled out at the top of 002:
-- outside the runner there is no ledger row, no advisory lock, no recorded
-- checksum, and no single transaction around the file.
--
-- It depends on nothing in 001 or 002. Neither table references issues,
-- workspaces or teams, so this file applies against an empty database as
-- readily as against a populated one -- which is exactly what the absence of
-- a membership table above is claiming.


CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- Stored already folded to lowercase, and constrained to be, rather than
    -- typed CITEXT.
    --
    -- The requirement is that two registrations differing only in case are
    -- one account. CITEXT delivers that at the type level and would still be
    -- the wrong trade here:
    --
    --   * It needs `CREATE EXTENSION citext`, which the linter forbids
    --     guarding with IF NOT EXISTS -- so this migration would fail outright
    --     against any database where the extension is already installed, and
    --     needs a privilege the application role has no other reason to hold.
    --   * It stores whatever case was submitted. Uniqueness would hold, but
    --     the canonical spelling of an address would be whichever variant
    --     happened to register first, so logs, outbound mail and any lookup
    --     written by someone who forgot to fold would each see a different
    --     string for one account.
    --
    -- Folding on the way in fixes both: the stored value IS the canonical
    -- value, a plain UNIQUE over lowercase text is already case-insensitive
    -- uniqueness, and its btree index serves the login lookup directly with
    -- no LOWER() around the column to defeat it. The cost is that original
    -- capitalisation is not recoverable, which for an address nobody displays
    -- back is not a cost at all.
    --
    -- The same shape as workspaces.slug in 002, and for the same reason: a
    -- constraint the database enforces beats a normalisation the application
    -- is trusted to remember.
    email TEXT NOT NULL,

    -- The argon2id encoded hash: algorithm, version, cost parameters, salt and
    -- digest in one string. Never a password, and never a reversible
    -- encoding of one.
    password_hash TEXT NOT NULL,

    -- Display name, optional. A person who has not given one is a person with
    -- no name to show, not a person named "".
    name TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Maintained by whoever writes the row, exactly as on issues: this schema
    -- has no touch trigger, here or anywhere.
    --
    -- Not an oversight and not a preference. A PL/pgSQL body is dollar-quoted,
    -- and tests/test_migration_lint.py rule 1 reads the BEGIN that opens the
    -- block as transaction control (hole #4, deferred 2026-09-02, with an
    -- xfail test naming it). Adding the trigger here means fixing that rule
    -- first. Until then the application owns the column, which is the same
    -- arrangement issues has lived under since 001.
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT users_email_key UNIQUE (email),

    -- What makes the UNIQUE above case-insensitive. Without it the folding is
    -- a convention, and one INSERT that skips it -- a fixture, a console, a
    -- future repository method -- creates a second account for an address
    -- that already has one, with every uniqueness check still reporting
    -- success. lower() is IMMUTABLE, so it is legal in a CHECK.
    CONSTRAINT users_email_lowercase
        CHECK (email = lower(email)),

    -- Deliberately permissive, and deliberately not RFC 5322. It rejects the
    -- shapes that are certainly not addresses -- no '@', more than one '@',
    -- nothing either side of it, embedded whitespace -- and stops there. Real
    -- validation is the service's job; this is the floor beneath it, so that
    -- a row can never hold something no mail system could ever accept.
    CONSTRAINT users_email_shape
        CHECK (email ~ '^[^@[:space:]]+@[^@[:space:]]+$'),

    -- 320 is the RFC 5321 ceiling: 64 octets of local part, '@', 255 of
    -- domain. Counted in characters rather than octets, which is fractionally
    -- more permissive for a non-ASCII address and is the point -- the service
    -- checks the same number the same way, so an over-long address comes back
    -- as a validation message a form can render rather than as a constraint
    -- violation, which is an internal error with nothing useful in it.
    --
    -- The floor of 3 is the shortest string the shape check can pass.
    CONSTRAINT users_email_length
        CHECK (length(email) BETWEEN 3 AND 320),

    -- The column can hold nothing but an argon2id hash. This is the one
    -- constraint here that guards against a category of bug rather than bad
    -- input: a code path that ever assigned a raw password, or a bcrypt hash,
    -- or an empty string to this column fails at the database instead of
    -- succeeding quietly and leaving credentials stored in a form nobody
    -- would notice until they were read back.
    --
    -- It does pin the algorithm: moving off argon2id needs a migration. That
    -- is the intended reading. Re-tuning cost parameters does not, because
    -- they live inside the encoded string after this prefix.
    CONSTRAINT users_password_hash_argon2id
        CHECK (password_hash LIKE '$argon2id$%')
);


CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    user_id UUID NOT NULL,

    -- The SHA-256 of the session token, raw bytes -- never the token.
    --
    -- A stolen database must not be a stolen set of live sessions, which is
    -- the whole reason a digest is stored at all. SHA-256 rather than argon2
    -- because the two hashes are protecting different things: a password is
    -- low-entropy and guessable, so its hash has to be slow. A session token
    -- is 32 bytes from secrets.token_urlsafe, so there is no guessable
    -- preimage space for a slow hash to defend -- and a salted argon2 hash
    -- could not be looked up by equality at all, which would turn every
    -- authenticated request into a scan of this table.
    token_hash BYTEA NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Stamped by the validation query on each successful use. NULL means the
    -- session has been issued but never presented since.
    last_used_at TIMESTAMPTZ,

    -- Absolute expiry, set when the session is issued. Enforced in the
    -- validation query rather than in Python, so that the server's clock
    -- decides and an expired row cannot be revived by an application bug.
    expires_at TIMESTAMPTZ NOT NULL,

    -- ON DELETE CASCADE, where 002 chose RESTRICT everywhere. The difference
    -- is what the child row means. An issue is data the product exists to
    -- keep, so losing a workspace's issues to one DELETE would be a
    -- catastrophe wearing the costume of a cleanup. A session is derived
    -- state -- a cached "this password was checked recently" -- which is
    -- worthless the moment its user is gone, and which RESTRICT would turn
    -- into an obstacle: deleting a user would fail until every one of their
    -- sessions had been deleted by hand, and a half-done account deletion
    -- that leaves live sessions behind is the worse outcome by a distance.
    --
    -- ON UPDATE RESTRICT for the same reason 002 gives: a user's id is its
    -- identity, and silently relocating sessions to follow a changed one
    -- would be a re-parenting nobody asked for.
    CONSTRAINT sessions_user_fk
        FOREIGN KEY (user_id)
        REFERENCES users (id)
        ON DELETE CASCADE ON UPDATE RESTRICT,

    -- Two jobs in one constraint. It is the index every authenticated request
    -- looks the token up through -- an equality probe on a unique btree -- and
    -- it makes a digest collision a rejected INSERT rather than two sessions
    -- that authenticate each other's owner. The second is unreachable in
    -- practice and is exactly the kind of thing to let the database refuse
    -- rather than to reason about.
    --
    -- No separate index on token_hash: this constraint already is one.
    CONSTRAINT sessions_token_hash_key UNIQUE (token_hash),

    -- A SHA-256 digest is 32 bytes, always. Anything else in this column is a
    -- different function's output -- a truncation, a hex string mistakenly
    -- encoded, a raw token -- and none of those should be storable.
    CONSTRAINT sessions_token_hash_length
        CHECK (octet_length(token_hash) = 32)
);


-- PostgreSQL indexes the referenced side of a foreign key, never the
-- referencing side, so without this the CASCADE above has to scan sessions in
-- full for every user deletion. It is also the index behind "revoke every
-- session for this user", which is what a password change and a "sign out
-- everywhere" both come down to.
CREATE INDEX sessions_user_id_idx ON sessions (user_id);

-- For the sweep that removes rows whose expires_at has passed.
--
-- No such sweep exists yet, and this index is created anyway. Logout deletes
-- a row; nothing else ever does, so from the first day this table takes
-- traffic it accumulates expired sessions that no query will look at again,
-- and a sweep is not a maybe. Adding the index in the migration that
-- eventually writes that sweep would mean a second migration for something
-- whose necessity is already settled -- and the wrong moment to discover the
-- scan is when the table is large enough for the sweep to matter.
CREATE INDEX sessions_expires_at_idx ON sessions (expires_at);
