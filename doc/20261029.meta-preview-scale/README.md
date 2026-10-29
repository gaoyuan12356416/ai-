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

Validation: 358 Python module tests, 19 actual-source DOM tests, syntax checks,
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
