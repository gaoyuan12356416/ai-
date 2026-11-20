# YouTube material uploader refresh

Sparse uploader queries cast `user_id` to text, preventing the existing
`idx_user_id` lookup. The production query exceeded its eight-second SQL budget.
Read the complete distinct uploader dimension through the existing covering
index (about 480 IDs), apply the existing text filter to that small dimension,
then join materials by numeric user_id. EXPLAIN confirms `idx_user_id` for the
filtered material lookup and PRIMARY for unfiltered/exact-ID reads. This works
with the current API process and takes effect through SQL hot reload.

Video scope, descending IDs, 100-row limit, product mapping, uploader option
permissions and submission revalidation remain unchanged. The LEFT JOIN retains
every material, including zero/null uploaders and deleted/missing admin users.
DISTINCT prevents duplicate material rows; uploader DTOs keep the same strings.

The picker also retained `cache.refreshing=true` after a failed poll. Clear that
state, preserve displayed/selected materials, invalidate the failed frontend
cache entry and offer retry. Ignore late results from an old uploader selection.

Validation: source/index/scope tests, existing Moboreels source and UI tests,
UI polling-error/retry/race cases, JS/Python syntax checks and production
read-only SQL plans plus complete material/metadata reads.

Deploy only the six targets in `deploy.py` from the exact pushed commit. Backups
and receipts live in `/mnt/data-disk/youtube-material-refresh-20261120/backup`.
Run `python3 ops/youtube-material-refresh-20261120/deploy.py install COMMIT`.
The `finish-install` action reconciles the earlier installed candidate against
its manifest, restores the unchanged source module and stops only this repair's
activation waiter. The already pending independent API rollout remains untouched.
Static assets and SQL switch atomically; no application restart, queue,
publication or ledger changes are required.

Rollback from the same checkout:
`python3 ops/youtube-material-refresh-20261120/deploy.py rollback COMMIT`.
It checks for later file drift and restores only backed-up config/static files.
Preserve current SQLite, media, schedules and all receipts.
