# X account and Facebook Page list loading

2026-11-13: the logged-in Chrome sample spent 38.0 s receiving the 221,346-byte
uncompressed X account list and 47.5 s waiting for publishing settings. X waited
for both and also awaited navigation configuration. The Page fast list took
11.6 s for 154,269 uncompressed bytes. These are individual samples, not SLAs.

Changes:
- Render X rows when the primary account read completes. Load publishing settings
  independently; update only their controls on completion. Preserve generation
  checks, abort replaced reads, show retry after failure, and keep backend auth.
- Move X navigation off the critical path. Start revenue/credit reads after rows
  are rendered. Unknown initial counters show a dash rather than zero.
- Load Excel modules only on demand on both list pages, including X's 951,904-byte
  parser. Existing Excel validation, frozen operations and receipts are unchanged.
- Enable gzip JSON only in the X list, X revenue portrait and FB portrait routes.
  Preserve all proxy destinations, Cookie checks, no-store headers and timeouts.
- Keep the current FB fast-list/background authorization flow. A slow or failed
  authorization read never relabels an unknown state as available.

Validation: `node ops/account-page-load/test_frontend.cjs` exercises ten cases,
including slow optional reads, failure/retry, stale responses, slow navigation,
lazy Excel and FB authorization fallback. Also check JS syntax, Python compile,
`git diff --check`, `nginx -t`, then the actual logged-in UI and Resource Timing.

Deploy only from the pushed commit into a separate checkout. The installer checks
the observed per-file hashes, backs up 10 static copies and 3 Nginx includes,
atomically replaces them, validates Nginx and performs a graceful Nginx reload.
Runtime static copies use the current public baseline (including newer macros and
permissions), so later source overlays cannot silently restore obsolete pages.
No application restarts, scheduler stops, database changes or test publication.

```sh
python3 ops/account-page-load/deploy.py apply \
  --folder /mnt/data-disk/account-page-load-20261113/deployment
python3 ops/account-page-load/deploy.py rollback \
  --folder /mnt/data-disk/account-page-load-20261113/deployment
```

Rollback restores only the saved files and gracefully reloads Nginx. It refuses
to overwrite subsequent edits. Current business databases, tokens, receipts,
leases, publishing schedules and task state remain untouched.
