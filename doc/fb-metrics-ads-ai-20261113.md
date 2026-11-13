# Facebook metric storage in ads_ai

The independent FB metric SQLite cache accumulated a complete material/day copy
on every refresh. The new adapter stores exact-decimal rows, immutable generation
identities and atomic day pointers in three dedicated `ads_ai` tables. It does not
change ad-source aggregation, material eligibility, publishing state or receipts.

`FB_AUTO_METRIC_STORAGE=mysql` selects a loopback metric-window client for the
unprivileged publisher. The root reader accepts only the existing product,
platform and at most 30 dates under the existing internal token; all reads use the
host FIFO and port 63350. The separate root metric refresh units read the same
source aggregation through FIFO and write only the dedicated tables on 63353.
Neither runtime path creates tables. On unavailable or incomplete data the prior
error contract is retained; there is no old SQLite fallback or source rebuild.
The writer uses a dedicated venv with `requirements-fb-metrics.txt`, compressed
MySQL protocol and one persistent connection/cursor per generation. FIFO readers
also enable wire compression for the cross-region connection.
An explicitly invoked migration may pass `--manual-reader` to use the reserved
operator read pool. This option is not available through runtime environment
settings; production readers always use FIFO. Transport failures are classified
without exposing SQL or credentials and retried at most three times. Integrity
failures are never retried as transport errors.

Amounts use DECIMAL(38,12). Imports reject precision loss. Each imported generation
is checked against the original ordered checksum, then read back through the
read replica and checked by normalized exact-decimal SHA256, row count and sums.
Only verified rows become ready; the pointer update rejects older refresh times.
Read windows use one repeatable-read transaction for pointers and totals.

Migration keeps all active day versions and every generation referenced by FB
runtime/dispatch JSON. Existing integer generation IDs remain unchanged. All
superseded generations remain in the separately verified private archive of the
original SQLite database; the migration manifest lists their IDs. The migration
does not delete or overwrite the source database. Source and deployment artifacts
must be archived by the storage workflow before local removal.

Deployment sequence:

1. Commit/push and fetch the exact release. Stage with
   `scripts/deploy_fb_metrics_ads_ai.py stage --folder DEPLOYMENT`.
2. Save/stop only the hourly/repair metric timers and let active metric refreshes
   finish. Generate `plan`, review the three-table DDL, then run `install` and
   `migrate` using `scripts/migrate_fb_metrics_ads_ai.py`; retain `migration.json`
   one directory above DEPLOYMENT.
3. Run a second reference plan and verify no referenced generation was missed.
   Compare the old/new three-day and thirty-day window IDs, exact totals and hash.
4. Apply the staged overlay. It preserves all unrelated runtime files, uses the
   existing scoped FB owner drain, and verifies/restores FB scheduler dependencies.
   The metric broker is separate; the main AI API is not restarted.
   Staging can preserve the reviewed concurrent Moboreels service overlay by
   changing only the metric factory anchor in its live service file. Other
   runtime files are copied from the actual current release, and source hashes
   are checked again before switching its pointer.
   The immediate pre-switch environment is saved privately. A failed drain
   restores its prior metric settings, and a post-switch failure records the
   actual source pointer with the matching backend for explicit recovery.
5. Restore the metric timers to their saved states. Verify the real read client,
   refresh unit and next triggers. Archive and remove the old metric file only
   after its processes have released it and the private archive verifies.

Rollback: retain the MySQL data. Restore the original SQLite archive to the
original metric path first if it was removed. Before making it authoritative,
restore all post-cutover MySQL generations, preserving their integer IDs and
generation keys, and synchronize the current day pointers. Verify those restored
rows and all active/history references against MySQL. Merely refreshing dates
would create different identities and is not a valid restore. The rollback tool
refuses a snapshot missing ready MySQL generation identities/counts or pointing
at older day versions. Run
`scripts/deploy_fb_metrics_ads_ai.py rollback --folder DEPLOYMENT` to restore the
previous code pointer/environment and remove the metric unit overrides. Restore
the saved metric timer states. Never restore the publication operational database.

Validation: `python -m unittest tests.test_fb_metric_mysql`; compile changed files;
source and read-replica per-generation checks; old/new window equivalence; scoped
service and scheduler-health acceptance. No test posts or messages are sent.
