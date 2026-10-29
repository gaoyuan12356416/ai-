# Large Meta asset previews

The previous whole-job 10,000-Ad cap discarded complete scans. Each selected
product now retains the existing 5,000-row keyset pages and complete watermark,
count, lineage and ownership checks, without a cumulative 10,000-row rejection.
Up to four product reads share the source's existing FIFO/SQL capacity.

Preview Meta reads use independent account workers under the eight-operation
budget, with at most 50 exact IDs per GET. Read data is consumed once, and an
unsupported or incomplete batch falls back to the ordinary node read. Execution
uses fresh clients and the original per-object checks, phase barriers, durable
claims, credentials, reference checks and uncertain-outcome fencing.

Task detail reads decode only the requested page and its Video account results,
with summary counts and page rows from one SQLite snapshot. Historical blocked
Videos still appear in the pending filter. Polling no longer locks page controls;
switching requests aborts the old read and ignores stale responses. Editing the
scope permits a new preview while the old task remains in history. Old cap errors
can be explicitly re-previewed with their frozen scope.

`scripts/run_meta_asset_scope_once.py` is an operator tool, not a timer. It binds
only the asset package to the verified release, uses the original owner's live
Cookie/module/product permissions, previews an explicit subset of the source
task's products, and submits an explicitly authorized execution through the main
API so all DELETEs share its existing budget. It never repeats an unknown write
or changes the operation ID. Results and SQLite evidence stay on the data disk.

Validation: 359 Python module tests, 19 actual-source DOM tests, syntax checks,
and a real-browser local 15,000-object fixture. No fixture test sends a deletion.
Live operator execution and final receipts are separate acceptance evidence.

Deploy via `scripts/deploy_meta_preview_scale.py prepare RELEASE`. Preserve its
printed backup, confirm no active deletion/recheck, stop only the main API,
apply the plan, then use `scripts/safe_restart_drama_api.sh`. Independent read-only
previews survive; main-process previews recover as interrupted, without writes.
Both HTML/JS copies change; shared navigation, other services and timers do not.

Rollback: with execution idle, stop `drama-material-api.service`, run
`python3 RELEASE/scripts/deploy_meta_preview_scale.py rollback BACKUP`, then
`bash RELEASE/scripts/safe_restart_drama_api.sh`. Keep the live SQLite ledger.
Restoring code never restores old task states or repeats DELETEs.

## Production verification

Deployed `16eb6d9e8277983264da8cac1e87b5ff3d1b990e` to
`43.166.187.96:/root/drama_material_service` at 2026-10-29 15:40 CST.
Only the main API restarted (PID 1339234 → 1594216); the independent
read-only recovery worker remained alive. All eight target hashes, including
the two public static copies, match the verified release. Public HTML and JS
return 200 with the new cache version. Owner-authenticated detail reads return
200, and the old main-process preview correctly recovers as interrupted.

Backup: `/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20261029T073951Z-16eb6d9e8277`.
API/public acceptance JSON is stored there. Deployment preserved the existing
360,020 object attempts, 332,381 receipts and 1,080 video-account attempts.
These are deployment checks, separate from subsequent operator-authorized
cleanup receipts.

Exact rollback, only after deletion/recheck workers are idle:

```bash
systemctl stop drama-material-api.service
python3 /mnt/data-disk/meta-ad-asset-delete/release-16eb6d9e8277/scripts/deploy_meta_preview_scale.py rollback /mnt/data-disk/meta-ad-asset-delete/recovery-backup-20261029T073951Z-16eb6d9e8277
bash /mnt/data-disk/meta-ad-asset-delete/release-16eb6d9e8277/scripts/safe_restart_drama_api.sh
```

Do not restore the evidence SQLite copy.

## Creative references and durable continuation

A large verified Creative set also exposed a 60-second source-reference timeout.
Verified references now split by exact account and at most 500 Creative IDs,
using at most four source readers. Cross-product references remain in the query;
unverified identities retain the original fallback. A live read-only probe
checked 8,377 Creatives across 127 accounts in 60.051 seconds, with 2,563 source
references and no out-of-scope Ad references. Meta verification is still required.
Blocked-object rechecks now use the existing bounded account GET workers, with
fresh authorization for each object and unchanged frozen IDs and write fences.

The follow-up release is `56b14d17c3dc42def600b2b22f8b763b3c83976c`.
As of 2026-10-29 16:47 CST it is staged, not yet loaded by the main API: the
operator-authorized Video phase is still running. The one-time
`meta-scope-continuation-20261029.service` waits for that run and every other
asset deletion/recheck to settle, backs up and replaces only source/service,
restarts only the main API, verifies hashes/health, then submits the frozen
recheck and Creative-only continuation through the normal API with stable IDs.
It has `Restart=no`; failures require checking the ledger before resuming.
The controller JSON records the actual commit, backup, PID and deployment stage.

The one-time `meta-scope-video-audit-20261029.service` waits for this continuation,
then uses GET-only complete account-video inventories to distinguish absent,
still-present and unverified associations after failed DELETEs. It preserves
the original DELETE ledger and writes a separate evidence file; an error alone
never becomes success. Script release: `825002529fc7bbd0552db4bd341b91c0c27b11c3`.
Neither service installs a timer or a recurring deletion job.

For this reference layer, use `scripts/deploy_meta_reference_scale.py prepare`
and the controller's recorded backup. To roll back after execution is idle,
stop the main API, run the same release's
`scripts/deploy_meta_reference_scale.py rollback BACKUP`, then restart with
`scripts/safe_restart_drama_api.sh`. Roll back this layer before attempting the
earlier full preview rollback. Retain the current SQLite database throughout.
