# X reauthorization and ownership (2026-10-29)

The callback rejected a valid X login when the X account belonged to another
backend user. The personal page collapsed this and platform suspension into
the same generic failure message. The operator explicitly changed the rule:
whoever successfully authorizes the X account becomes its backend owner.

The existing Feishu Cookie/module gate, expiring one-use PKCE state, X identity
verification and per-account locking remain in force. No owner ID is accepted
from the browser. Successful authorization updates all four owner fields and
the authorization actor from the trusted state. Existing account ID, first
authorization time, language, publish approval, schedules and publish history
are preserved. There is no bulk reassignment of historical failed callbacks.
The personal list, verify and disable operations remain scoped to the owner.

An ownership change inserts paired `ownership_transfer` events (`released`
with the old actor, `acquired` with the new actor) in the same SQLite transaction
as the account update. Failed X identity verification changes neither owner nor
stored credentials. A storage failure rolls back the ownership/audit transaction
and restores the previous credential file. The same owner's display metadata is
refreshed on login without a transfer event. No secrets enter audit events.

The authorization page explains the rule and maps only known reason codes to
fixed Chinese messages. Platform-suspended accounts must be restored on X before
authorization can succeed. Arbitrary callback reason strings are never rendered.

## Validation

```sh
python scripts/test_x_accounts.py -q
python scripts/test_x_app_switch.py -q
node scripts/test_x_oauth_notice.js
git diff --check
```

Tests cover cross-user and cross-tenant identity replacement after verified
OAuth, account settings/first-authorization preservation, old-owner access
rejection, paired transfer auditing, suspension and transaction rollback,
legacy-owner recovery, one-use state and current-App credential binding.
The UI test executes the actual notice function for nine outcomes and compiles
all inline scripts. Tests use isolated data and mocked upstream authorization.

## Deployment and rollback

Run `python3 scripts/deploy_x_reauth_ownership.py` from the clean GitHub checkout
for read-only preflight, then add `--apply`. The installer checks exact live
baselines, copies the current composite and overlays only the OAuth module and
personal HTML. It drains publishers, takes existing runner locks, backs up live
SQLite/credentials/environment/static assets, switches the immutable release,
checks data/credential invariance at cutover, and starts OAuth plus its previously
active Auto dependency. Main API code/PID is unchanged. Original triggers and
the personal pool scheduler are restored after health readiness.

The command prints a private backup directory containing `manifest.json`.
Rollback is `python3 scripts/deploy_x_reauth_ownership.py --rollback BACKUP --apply`
from the retained GitHub checkout. It drains the same workers, restores only
the previous release and two HTML files, and preserves current owners, tokens,
databases and publish history. It refuses to replace a subsequently deployed
release. Do not rerun the application-switch installer or restore old tokens.

Deployment never performs a live OAuth login or creates a verification Post.
Successful synthetic callback tests do not replace a real user's subsequent
X login; the user's next normal authorization is the live acceptance point.
