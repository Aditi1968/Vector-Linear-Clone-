-- DRAFT. NOT A MIGRATION YET. Deliberately not in migrations/.
--
-- The container's start command runs `python -m scripts.apply_migration --all`,
-- which applies everything matching `migrations/*.sql`. Putting this file
-- there and pushing it would apply it to the staging Neon branch on the next
-- deploy, without anybody choosing to. It lives here until it is approved,
-- exactly as CLAUDE.md says `schema_drafts/` is a design document rather than
-- an executable migration.
--
-- To adopt: move to `migrations/033_google_identity.sql`, add the filename to
-- EXPECTED_MIGRATIONS in tests/test_phase1a2_gates.py (that list is asserted
-- exactly, so the diff of any branch adding a migration says so where a
-- reviewer reads it), then apply through the runner. Nothing else may apply it.


-- Google sign-in, under the rule that a Google sign-in never joins an
-- account somebody already holds a password to.
--
-- WHY THAT RULE, because it is the whole shape of this file. Vector does not
-- prove that a registering address belongs to whoever typed it --
-- `app/services/auth.py` says so in as many words: "address verification is
-- still [a later phase]". Auto-linking a Google identity onto a matching
-- email would therefore be a pre-registration account takeover:
--
--   1. an attacker registers victim@gmail.com with a password of their own,
--      which succeeds because nothing checks the address;
--   2. the victim later signs in with Google;
--   3. auto-linking joins the two, and the attacker's password now opens the
--      victim's account and every workspace in it.
--
-- So the two never merge here. The existing UNIQUE on `users.email` is what
-- enforces it, and enforces it in the database rather than in a service that
-- could forget: a Google sign-in for an address that already exists fails on
-- that constraint, and the REST layer turns the violation into "that email
-- already has a Vector password -- sign in with it". No new mechanism, and no
-- code path that can be reordered into linking by accident.
--
-- Linking is still reachable later and is deliberately left out: it needs the
-- user to prove BOTH sides, which means a password sign-in followed by an
-- explicit connect from Settings. That is a feature, not a default.


-- 1. A Google-only account has no password, so the column has to admit NULL.
--
-- DROP NOT NULL rather than a default: there is no argon2id string that means
-- "no password", and inventing one -- a hash of a random secret nobody holds
-- -- would make `password_hash IS NULL` untrue while the account still cannot
-- be signed into with a password. The honest representation of "no password"
-- is no password.
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;


-- 2. The format check, widened to admit that NULL.
--
-- DROP then ADD under the same name, which is how 004 and 009 widen a CHECK
-- and is the only way Postgres offers. The narrowing half is unchanged: a
-- value that IS present must still be an argon2id hash, so this relaxes what
-- may be absent and nothing about what may be stored.
--
-- Worth being explicit that this is not merely cosmetic. Without the widened
-- CHECK, step 1 alone would still reject every Google-only insert, because a
-- CHECK on a NULL column evaluates to NULL -- which passes -- only if it is
-- written to. `password_hash LIKE '$argon2id$%'` against NULL is NULL, so it
-- would in fact pass; this is spelled out anyway so the intent is readable
-- rather than resting on three-valued logic nobody should have to re-derive.
ALTER TABLE users DROP CONSTRAINT users_password_hash_argon2id;

ALTER TABLE users ADD CONSTRAINT users_password_hash_argon2id
    CHECK (password_hash IS NULL OR password_hash LIKE '$argon2id$%');


-- 3. Google's stable identifier for the account.
--
-- The `sub` claim, and NOT the email. Google documents `sub` as the only
-- claim that never changes for a Google account; an address can be changed by
-- its owner, and keying on it would mean a person who renamed their Google
-- account either lost their Vector account or, worse, arrived at somebody
-- else's if the old address were later reissued. `email` stays as the human
-- label and the uniqueness key; `sub` is the credential.
--
-- Nullable because most accounts are password accounts and have no Google
-- identity. TEXT rather than BIGINT: `sub` is documented as an opaque string
-- that merely happens to be digits today, and storing an opaque identifier in
-- a numeric type is a bet on a format the issuer never promised.
ALTER TABLE users ADD COLUMN google_subject TEXT;


-- 4. One Google account is one Vector account.
--
-- Without this, a second row could carry the same `sub` and the lookup that
-- resolves a sign-in would have two answers -- which is not a duplicate-data
-- problem but an authentication one: whichever row the query happened to
-- return would be signed in.
--
-- UNIQUE over a nullable column is the right shape here. Postgres treats
-- NULLs as distinct by default, so every password-only account (all of them
-- today) coexists happily; the constraint binds only rows that actually carry
-- an identity.
ALTER TABLE users ADD CONSTRAINT users_google_subject_key UNIQUE (google_subject);


-- 5. A bound on the value, for the same reason `auth_rate_limits` bounds its
-- subject: an unbounded string arriving from outside is a row somebody else
-- chooses the size of. Google's `sub` is currently 21 digits; 255 is generous
-- and still finite.
ALTER TABLE users ADD CONSTRAINT users_google_subject_shape
    CHECK (
        google_subject IS NULL
        OR length(google_subject) BETWEEN 1 AND 255
    );


-- 6. Every account can be signed into by at least one means.
--
-- The state this forbids is a row with neither a password nor a Google
-- identity: unreachable by its owner, invisible as a bug, and reachable by a
-- future disconnect-Google feature that forgets to check whether a password
-- exists first. Writing it down here means that feature fails loudly at the
-- database instead of silently orphaning an account.
--
-- Validated against existing rows as it is added, which is safe: every row
-- today has a password_hash, because until step 1 the column could not be
-- NULL.
ALTER TABLE users ADD CONSTRAINT users_has_a_credential
    CHECK (password_hash IS NOT NULL OR google_subject IS NOT NULL);
