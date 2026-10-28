# 803 to 916 asset permission reconciliation

One-shot operational CLI in `scripts/meta_asset_permission_sync.py`. It does not
change the web application, scheduled tasks, token records, or the source database.
Credentials are read in memory from the existing CPU read-only credential source.
The legacy config file is parsed as AST and never imported/executed. Credential
values, HTTP exception URLs and paging URLs must not enter reports.

## Authorized scope

Source local user 803, destination 916, 360-Mobimagic BM 163392938074395.
Resolve current identities using `/me` and BM `system_users`; the stored 916
Facebook ID is stale. Grant asset-level full management to missing/insufficient
assets in 803's verified scope. Do not change the destination's BM member role.
Exclude disabled/inactive backend accounts, abnormal/forbidden/unpublished Pages,
unknown status, incomplete listings, and assets without proven delegation rights.
Preserve the target's pre-existing additional tasks. No downgrade fallback.

Unfiltered administrator-facing `assigned_pages` currently returns Graph code 1
even with minimal fields and limit 5. Discover source IDs through the authenticated
source's fully paged `me/accounts`, then read both users' administrator assignment
tasks with the official `pages` ID filter (50 IDs per call). Reject a response
containing IDs outside that filter. Cross-check source tasks and target presence
against each token's own list. Page publication state and eligible account
delegation tasks are fetched with at most eight concurrent GET requests, durable
partial checkpoints, and no credential rotation. Restriction/rate responses stop
new reads. Administrator Page
`assigned_users` reads require an unavailable OAuth scope; read tasks from the
system-user `assigned_pages` edge instead. No OAuth scopes are changed.

## Run and audit

Run on CPU server using Python 3.9+ with requests and pymysql. `--run-dir` must be
on the verified data disk. All commands require `--code-version COMMIT`.

1. `preview --run-dir /mnt/data-disk/meta-permission-sync/803-to-916-RUN`
2. Inspect `manifest.json`, `manifest.sha256`, `comparison.csv`, and identity proof.
3. `execute --run-dir SAME_DIRECTORY` consumes that immutable manifest.
4. `resume --run-dir SAME_DIRECTORY` only reads back unknown submissions. A
   restricted BM stays paused; a new approved run after platform resolution is
   required instead of deleting restriction events or changing tokens.

After an interrupted preview, `preview --reuse-complete-snapshots` can reuse this
run's complete read checkpoints younger than one hour after revalidating the two
identities. Partial pages are never saved as complete snapshots. Every write
still rechecks live asset state and source/target assignment before submission.

An exclusive file lock protects the run directory. The fsynced journal records
each intent before submission, sanitized response and verified result. Writes
are serial, >=5 seconds apart globally and >=30 seconds per token. Every saved
unique credential is considered and relevant BM ADMIN status is verified live.
Discovery may reuse this run's fingerprint-matched checks for 30 minutes; each
selected acting administrator is revalidated before its first submission.
Page preflight reads the current owning business together with publication state;
only absent ownership metadata falls back to the BM-owned Page list. A source
assignment invisible to one administrator causes another verified administrator
to check it, rather than being interpreted as removed. Resume may reconsider old
visibility skips only if the journal contains no write intent for that asset.
368/1390008 and rate-limit responses pause the BM without switching tokens.
Credential/permission errors may use the next verified administrator only when
the API explicitly rejected the request. Timeouts/5xx require readback and never
trigger automatic resubmission. Failed canaries pause their asset class.

Success requires both administrator assignment readback and target-token access
verification. JSON and UTF-8 CSV reports distinguish confirmed success, pending
verification, abnormal/unknown/skipped assets, errors and unattempted candidates.
No real post or advertisement is created for verification.

## Deployment and rollback

Test locally, commit/push to GitHub, and check out that commit in a dedicated CPU
operational checkout. Production services do not need restart. Preserve the old
grant scripts and their ledgers. Keep evidence outside Git.

Stop the dedicated process to stop further submissions. Code rollback uses the
previous recorded commit and does not undo Meta grants. Asset permission rollback
requires reviewing each confirmed intent: restore its exact `before_tasks`, or
remove only the newly created assignment if no assignment existed before. Never
remove another actor's later-added tasks or delete an entire user's access blindly.

Tests: `python -m unittest discover -s scripts -p test_meta_asset_permission_sync.py`.

## Follow-up investigation and controlled recovery

`scripts/meta_asset_permission_followup.py --run-dir NEW_DIRECTORY --prior-run
PAUSED_DIRECTORY --code-version COMMIT` performs a fresh read-only audit. It reads
`permitted_tasks` from the BM's complete `client_ad_accounts` and `client_pages`
lists, rather than treating one administrator's `user_tasks` as the BM sharing
ceiling. Assets absent from those lists remain unknown unless current ownership
proves authority. It does not modify the owner-to-BM sharing arrangement.

Page permission comparison uses the same `assigned_pages` edge through both
the system user's own token and a verified administrator. `me/accounts` remains
the actual visibility check; its effective task list is retained separately.
Those two different edges may differ on leads/revenue tasks. Same-edge conflicts
are checked with other verified administrators and remain unknown if unresolved.
State failures take precedence over task discrepancies.

New Page grants can expose either `PROFILE_PLUS_FULL_CONTROL` or an expanded
bundle containing management, Facebook access, ads, analytics, content, messages,
moderation, leads, and revenue tasks. Require the complete bundle, not `MANAGE`
alone. Preserve existing target tasks. The task names follow Meta's official
[Page SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/page.py);
the expanded share bundle was also cross-checked against existing system-user
management assignments during the read-only investigation.

The new manifest links the immutable prior restriction, original actor fingerprint,
target identity, and restricted asset. Its first authorization must use that exact
actor and asset; a successful acknowledged response and both readbacks establish
recovery for that attempt. Elapsed time, ordinary GET success, or a different
administrator cannot establish recovery. Any failure or ambiguous outcome keeps
the BM paused. Resume of an interrupted recovery reads back first and requires a
previously acknowledged write before releasing the queue. No prior restriction
event is deleted. Ordinary follow-up writes are serial, at least 30 seconds apart
globally and 300 seconds per credential.

For format-v2 manifests, every write rechecks both users' assignment through the
acting administrator and their own tokens. Current shared authority is reread via
the asset's `agencies.permitted_tasks`; Page agency reads derive a Page token only
in memory. Derived credentials are never persisted or logged. Test the complete
operation with `python -m unittest discover -s scripts -p 'test_meta_asset_permission*.py'`.
