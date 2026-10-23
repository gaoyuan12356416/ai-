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
9695b734 runtime baseline; installs source_duration.py, sources.py, picker JS/HTML
to the main API/X runtime/public static as appropriate; snapshots code, SQLite
and token state; drains publishers; restores the original service/trigger states;
and compares ledger/token fingerprints during cutover. It does not restore data
on rollback. Use the same checkout and script with --rollback <manifest.json>.

Production receipt and browser acceptance will be appended after deployment.
