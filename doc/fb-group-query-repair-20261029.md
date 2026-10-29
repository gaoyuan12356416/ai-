# FB group pool source-query repair and operator recovery

Both 2026-10-29 scheduled runs reached the eighth broker read (drama resource
metadata), then received HTTP 400 after a slow read. The broker caught
TemplateError as ValueError, incorrectly classifying runtime failures as invalid
requests; the original low-level database error was discarded and is unavailable.

The first 100 candidates returned 2,122 resource rows, but only 21 different
metadata records (1,694,870 versus 15,739 JSON bytes). A read-only production
comparison verified the same byte-exact metadata digest before and after the
change. The repair selects DISTINCT binary values for all six metadata fields,
removing repeated episode metadata and unnecessary sorting. BINARY is essential:
case/accent/space differences must still reach the existing ambiguity checks.
The source/product/identity/language/release and eight-second SQL limit remain.

The broker now preserves classified runtime failures as HTTP 503. The SQL adapter
records only a numeric MySQL error and sanitized error category, never SQL,
credentials or raw stderr. Group-pool SELECTs retry classified transient errors
at most once. Data-validation errors and platform writes are never retried here.

Deployment: `scripts/deploy_fb_group_query_repair.py --apply`, from the exact
GitHub commit. It backs up six files and the operational database, drains the
existing workers, restarts the main broker and FB sidecar, and restores the prior
service states. There is no schema migration or schedule change. Rollback:
`python3 <release>/scripts/deploy_fb_group_query_repair.py --rollback <manifest>`.
Rollback restores code only; never restore the database or published links.

Explicit recovery uses `scripts/recover_fb_group_pool_run.py --run-id N
--expected-revision R` (dry run). `--apply --backup <new-data-disk-path>` creates a
separate recovery run using the original item boundary and current trusted
publisher, sources, Page scope and occupancy checks. It only accepts unwritten
source-failure runs on the same Beijing day and unchanged schedule revision.
The original skipped run stays unchanged. New tasks, reservations and a durable
deterministic operation receipt commit together. Repeating the operation returns
the same batch. Publication remains with the ordinary worker and its existing
unknown-outcome safeguards. A new dry run is required for the second skipped run
after reserving the first, so the same materials cannot be allocated twice.

Validation includes the real broker SQL dialect, transient recovery/non-retry,
metadata ambiguity preservation, error redaction, and atomic/idempotent recovery.
