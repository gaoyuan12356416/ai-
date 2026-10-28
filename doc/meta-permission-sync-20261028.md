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

The source user's administrator-facing `assigned_pages` currently returns Graph
code 1 even with minimal fields and limit 5. Use the authenticated source's fully
paged `me/accounts` with `tasks` as source permission evidence; explicitly record
this limitation in the manifest. The target's administrator assignment list is
cross-checked against its own visible list. Page publication state is fetched in
separate Graph envelopes containing GET operations only. Administrator Page
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
