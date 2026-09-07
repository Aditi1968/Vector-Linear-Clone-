-- A workspace's claim on a GitHub installation, separated from proof that the
-- claim is true.
--
-- 013 stored one thing where there are two. `github_installations` held the
-- installation id the setup redirect handed back, and the row's existence was
-- taken to mean the workspace had installed the app. It does not mean that.
-- The id arrives in a query string, from a browser, and the only thing the
-- state cookie proves is that the same browser started an install here -- not
-- that the installation it names is the one that browser just created. GitHub
-- numbers installations with a small ascending counter, so `?installation_id=N`
-- for somebody else's N is a guess anyone can make, and 013's
-- `github_installations_installation_id_key` then made the guess permanent:
-- first claim wins, every later webhook for N routes to the claimant, and the
-- organisation that actually holds N is refused for good.
--
-- The only party that can settle which workspace an installation belongs to is
-- GitHub, and the only thing in this system GitHub has signed is a webhook
-- delivery. So the row now carries the difference between "a workspace said
-- this" and "GitHub said this", and everything the product reads is keyed off
-- the second.
--
-- What that leaves is a two-step link:
--
--   1. the install callback records a CLAIM -- workspace, installation id,
--      who claimed it, when -- and `confirmed_at` stays NULL. The workspace
--      is PENDING and nothing about the installation's account or its
--      repositories may be written against it;
--   2. a delivery whose HMAC verifies against the deployment's webhook secret,
--      naming that same installation id while the claim is still young,
--      sets `confirmed_at`. Only then is the link real.
--
-- The window is what does the work, and it is worth being exact about why. A
-- signed webhook proves GitHub sent it; it does not prove the claimant owns
-- the installation. What the claimant cannot do is make GitHub emit
-- `installation.created` for an organisation they have no access to. So a
-- claim on an installation that was created months ago is never confirmed by
-- anything -- the events that would confirm it have already been delivered and
-- dropped -- and it expires holding nothing. The attack is reduced from "name
-- any id, ever" to "predict an id and race a live install within minutes",
-- which is the ceiling `GET /app/installations/{id}` would remove and this
-- deployment cannot reach: that call needs RS256 JWT signing and an HTTP
-- client at runtime, and this application deliberately carries neither.
--
-- No expiry COLUMN, deliberately. The deadline is a policy, not a fact about
-- the row -- `app.services.github.CLAIM_TTL` -- and a column would freeze the
-- policy at the instant each claim was written, so shortening the window would
-- leave every claim already in flight on the old one. `connected_at` is
-- rewritten on every claim (GithubService.connect deletes and re-inserts), so
-- "young enough" is a predicate over a column that is already here.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/016_github_installation_trust.sql`. The runner wraps the file in
-- one transaction, takes an advisory lock and records a checksum; a hand-run
-- gets none of that and executes statement-at-a-time in autocommit, which here
-- would mean the CHECK below arriving before the backfill that makes existing
-- rows satisfy it -- a migration that aborts halfway with the column added,
-- nothing recorded, and every pre-existing installation reading as unconfirmed.
--
-- DEPENDS ON 013, which creates `github_installations` and the
-- `account_login` column this file backfills from. Applying it before 013
-- fails on a table that does not exist and -- inside the single transaction
-- the runner wraps the file in -- leaves nothing behind. That failure IS the
-- dependency check: the ledger records what has been applied and not what
-- depends on what.


-- When GitHub confirmed the claim, or NULL while it is still only a claim.
--
-- Nullable and staying nullable: "unconfirmed" is the state every installation
-- passes through, not a missing value to be backfilled. A boolean would say
-- the same thing and lose the one fact worth auditing afterwards, which is
-- WHEN the confirmation landed -- the difference between a webhook that
-- answered the claim within seconds and one that turned up at the edge of the
-- window.
--
-- Not `NOT NULL DEFAULT now()`, which is the spelling that would quietly
-- reintroduce the defect: every claim would be born confirmed.
ALTER TABLE github_installations ADD COLUMN confirmed_at TIMESTAMPTZ;


-- Rows written before this migration, judged by the one signal they carry.
--
-- Under 013's code `account_login` had exactly one writer:
-- `GithubService._apply_account`, reached only from a delivery whose signature
-- had already been verified against the webhook secret and which resolved to
-- this row by installation id. A non-NULL login is therefore a record that
-- GitHub did name this installation to this deployment -- which is precisely
-- the confirmation this migration invents, applied retroactively rather than
-- guessed at.
--
-- `updated_at` and not `now()`: that column was last written by the delivery
-- that set the login, so it is when the confirmation actually happened. Using
-- now() would date every historical confirmation to the migration.
--
-- A row with no login is left unconfirmed, and that is not a downgrade of
-- something that used to work -- it is the honest reading. Nothing ever
-- verified it. It reports PENDING until a confirming delivery arrives or an
-- admin re-runs the install, and the CHECK below then holds it to writing
-- nothing in the meantime.
UPDATE github_installations
SET confirmed_at = updated_at
WHERE account_login IS NOT NULL;


-- An unconfirmed claim may hold nothing GitHub told us.
--
-- This is the leak, closed in the schema rather than left to the writer to
-- remember. `account_login` is somebody's organisation name and it arrives
-- only from a webhook, so a claim that was never confirmed accumulating one
-- would mean an attacker who guessed an installation id could read the victim
-- organisation's name straight out of `githubIntegration { installation {
-- accountLogin } }` -- which is the defect in miniature, surviving the fix.
--
-- It also guards the repositories, which have no constraint of their own.
-- `GithubService.apply_webhook` writes the account before it writes any
-- repository row, and both happen in one transaction, so a delivery that
-- reached an unconfirmed row would abort here -- before a single private
-- `full_name` from the victim's organisation was inserted. That ordering is
-- load-bearing and is stated in that method; if it ever stops holding, the
-- repositories need a constraint of their own rather than this one's shadow.
--
-- Spelled as "confirmed, or blank" rather than as two CHECKs, because the two
-- halves are one rule: what is readable about an installation is exactly what
-- GitHub has confirmed.
ALTER TABLE github_installations
    ADD CONSTRAINT github_installations_unconfirmed_holds_no_account
    CHECK (confirmed_at IS NOT NULL OR account_login IS NULL);
