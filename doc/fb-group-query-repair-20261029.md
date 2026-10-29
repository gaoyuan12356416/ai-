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

## Production acceptance

- Runtime commit: `2fa432e7600f767185093daa257843e202cca70c`, deployed on
  `43.166.187.96` at 2026-10-29 12:17 Beijing time.
- Release: `/mnt/data-disk/fb-video-publish/releases/group-query-2fa432e`.
- Backup: `/mnt/data-disk/fb-video-publish/backups/group-query-20261029T041645Z-2fa432e7/manifest.json`.
- Six installed hashes match. Main API, FB sidecar, group scheduler, manual
  worker and the temporarily paused prepare timer are active. Existing video
  and group facts were compared before service reopening; no database restore
  or migration occurred. The first attempt exited before replacing files while
  GPU work was active; existing jobs completed normally before activation.
- Post-deployment real-source check: 100/100 materials valid, zero errors;
  the resource read returned 21 byte-distinct metadata records.
- Explicit recovery created two separate runs with 234 and 159 tasks. Original
  failed runs, schedule revision and effective time are unchanged. A repeated
  read of each deterministic operation returned its original batch; no duplicate
  material assignments exist. Fifteen pool materials already belonged to older
  automatic tasks (nine planned, six skipped) and retained the existing policy.
- At 12:20 Beijing, 22 were confirmed published, one had exhausted eight Page
  authorizations with explicit Meta 368/190 refusals, and 370 were still queued,
  executing or processing at Meta. Enqueueing is not completion. Result follow-up
  tracks only these accepted batches and never automatically retries unknowns.

Exact code-only rollback:

```sh
python3 /mnt/data-disk/fb-video-publish/releases/group-query-2fa432e/scripts/deploy_fb_group_query_repair.py --rollback /mnt/data-disk/fb-video-publish/backups/group-query-20261029T041645Z-2fa432e7/manifest.json
```
