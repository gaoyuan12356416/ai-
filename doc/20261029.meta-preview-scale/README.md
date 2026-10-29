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
