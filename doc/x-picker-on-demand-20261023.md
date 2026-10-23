# X picker: explicit search and absent duration recovery

The picker opens without a material request. Product, language and text changes
clear stale visible results but preserve staged selections. Search/Enter starts
the request; blank search browses the chosen product. Pagination remains explicit.

The reported ID 7781416 returned HTTP 200 at 14:50:52 Beijing time on 2026-10-23,
but its source video_duration was 0 and the catalog adapter rejected it before
the existing duration recovery ran. A read-only reproduction took 6.333 seconds
for three SQL reads and returned the missing-duration message. HTTPS MP4 metadata
measured the actual video at 1792.934 seconds in 3.588 seconds.

Exact-ID searches and explicit selected-ID revalidation now use the existing
MP4 metadata reader through a metadata-only entrypoint. They normalize legacy
HTTP URLs to HTTPS, retain the host allowlist/strong ETag/video-track/range checks,
and share one 8-second metadata budget across the selected IDs (bounded socket
reads can finish after that budget). They never fall back to full downloads.
Success uses the existing 15-minute bounded URL/allowlist cache and still passes
the normal drama, language, deployment, duration and final-publish validation.
The source database is not updated. Ordinary catalog browsing does not batch
probe videos; its missing-duration message suggests an explicit ID search.

Validation before production: 112 personal-publishing backend tests, 13 source
duration tests and 16 shipped-script frontend cases passed. JavaScript syntax
and git diff whitespace checks passed. No new Post, queue, pool item or historical
retry is part of acceptance.

Deploy with scripts/deploy_x_picker_on_demand.py from the exact pushed branch
codex/x-material-picker-on-demand-20261023. The installer checks the previous
aa252f2 runtime baseline for the final metadata-budget adjustment (the first
installation used 9695b734); installs source_duration.py, sources.py, picker JS/HTML
to the main API/X runtime/public static as appropriate; snapshots code, SQLite
and token state; drains publishers; restores the original service/trigger states;
and compares ledger/token fingerprints during cutover. It does not restore data
on rollback. Use the same checkout and script with --rollback <manifest.json>.

## Production receipt

- Final code: 37de345e74afd9881ca14d86f23652c246570f28, fetched from GitHub.
- CPU: 43.166.187.96; main runtime /root/drama_material_service;
  X runtime /opt/x-post-automation/current; public static /usr/share/nginx/html.
- Source checkout: /mnt/data-disk/x-personal-publishing/picker-on-demand-37de345.
- Backup manifest: /mnt/data-disk/x-post-automation/backups/20261023T070945Z-source-37de345e/manifest.json.
- Guarded deployment reported deployed. All 10 installed hashes match. Ledger
  and token fingerprints were preserved. All eight trigger states were restored;
  main API, X sidecar, X Auto dependency and personal pool scheduler are active.
- Linux independently passed the same 112 backend and 13 duration tests.
- Read-only candidate acceptance: exact 7781416 under the social product returned
  the English drama Falling for My Dad's Billionaire Friend at 1792.934 seconds
  in 9.546 seconds; under Dramawave it returned the same item in 6.351 seconds.
  Explicit-ID revalidation succeeded without errors in 6.638 seconds.
- The deployed browser picker opens with the instruction to click Search, and
  remains idle after typing 7781416. The browser client's Nginx material-read
  entry remained at the prior 14:51:27 request until Search was clicked.
- A cold browser read exposed an unnecessarily short 3-second per-request
  metadata cutoff. The final version uses the remaining 8-second shared budget,
  retaining all byte/host/range checks. Three uncached production-file metadata
  reads then passed in 3.005, 3.020 and 3.213 seconds. The final restarted API's
  cold browser query returned 7781416 with the English title and 1792.934 seconds.
  The picker remained at zero selections; no test publication was created.
- Browser screenshots and sanitized deployment receipt are saved locally in
  D:/codex/artifacts/x-picker-on-demand-20261023/.

Rollback from the checked-out deployment on the CPU server:

```sh
cd /mnt/data-disk/x-personal-publishing/picker-on-demand-37de345
python3 scripts/deploy_x_picker_on_demand.py --rollback /mnt/data-disk/x-post-automation/backups/20261023T070945Z-source-37de345e/manifest.json
```

This restores only the preceding four code files, retaining current publisher
data and credentials.
