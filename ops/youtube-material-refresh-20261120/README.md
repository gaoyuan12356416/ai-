# YouTube material uploader refresh

Sparse uploader queries cast `user_id` to text twice, preventing the existing
`idx_user_id` lookup. The production query exceeded its eight-second SQL budget.
Keep the SQL projection numeric and compare the validated numeric uploader
directly. Video scope, descending IDs, 100-row limit, product mapping, uploader
option permissions and submission revalidation remain unchanged. Unknown
uploader filtering still includes zero/null.

The picker also retained `cache.refreshing=true` after a failed poll. Clear that
state, preserve displayed/selected materials, invalidate the failed frontend
cache entry and offer retry. Ignore late results from an old uploader selection.

Validation: source/index/scope tests, existing Moboreels source and UI tests,
UI polling-error/retry/race cases, JS/Python syntax checks and production
read-only SQL plans plus complete material/metadata reads.

Deploy only the six targets in `deploy.py` from the exact pushed commit. Backups
and receipts live in `/mnt/data-disk/youtube-material-refresh-20261120/backup`.
Run `python3 ops/youtube-material-refresh-20261120/deploy.py install COMMIT`, then
`activate COMMIT`. Activation reuses an already pending API restart; it never
interrupts the existing drain. Restore/check the YouTube scheduler snapshot.
Static assets and SQL switch atomically; no queue, publication or ledger changes.

Rollback from the same checkout:
`python3 ops/youtube-material-refresh-20261120/deploy.py rollback COMMIT`.
It checks for later file drift, restores only backed-up code/config/static files,
and activates the old source through the normal drain. Preserve current SQLite,
media, schedules and all receipts.
