# Material-pool notes and drama-language validation

Published pool rows previously rendered a schedule-wide capacity note in the
item error column. The page now keeps that note in a neutral, collapsed
`批次说明` disclosure beside the queue. Successful items have no error styling;
actual material/publish errors and unknown outcomes remain visible. Historical
run messages and publication records are preserved.

Pool admission, material scheduling and explicit operator-manual hydration now
resolve `ads_drama_resource` by the material's exact `data_source_id`. The
unambiguous drama language drives account routing and the delivery-time lookup.
Missing or stale `ads_custom_source.language` no longer rejects those paths.
Missing, malformed or conflicting drama languages have distinct errors. Alias
`jp` is canonicalized to `ja`, and all matching platform delivery times retain
the latest-time gate. No source MySQL record is updated. Existing frozen queues
and the separate automatic-template/legacy selection policies are unchanged.

Historical metadata and drama-language errors remain invalid until successful
revalidation; candidate enumeration alone does not clear them. The existing
no-post media backfill performs full source, delivery-time and media checks on
an explicitly supplied ID list. It cannot create a queue, Post or Repost.

The HTML baseline is the verified production version from upstream commit
`be822e367ec29823c2acd28daf8bb2b0a2d95461`, retaining recent manual-run controls
and pool summaries. The deployed composite's Python files match this branch's
baseline after line-ending normalization.

Validation: 138 selector/pool/manual/scheduler/bridge tests, plus 147 schedule
store tests. Browser verification uses a local isolated page with real read-only
production DTOs for a successful row, a material error and an unknown outcome;
it checks collapsed notes and preserved real errors, then query/reset behavior.
Production acceptance must also verify exact public HTML, both Python runtime
hashes, service health, unchanged publish ledgers and restored timer states.

Deploy only after pushing the exact commit. Fetch/archive that commit on the
CPU host, then run `install.py --source SOURCE --commit COMMIT --backup BACKUP`
with one `--material-id ID` for each explicitly scoped historical row. Keep IDs,
tokens, databases and receipts out of public Git. The installer rejects runtime
drift, drains publishers, takes the shared lock, backs up SQLite/code and token
fingerprints, stages the verified composite and overlays only the changed
files. It restarts the X, X Auto and main API services, revalidates the scoped
rows without publishing, verifies ledger preservation, and restores timers in
`finally`. A rescue timer restores original active timers after an interruption.

Rollback: stop the previously active X timers, wait for publisher services and
in-flight Sidecar requests to finish, and hold `/run/x-post-daily/runner.lock`.
Switch `/opt/x-post-automation/current` atomically to `previous_release` in the
private manifest. Restore the four files under `BACKUP/main/` to
`/root/drama_material_service/` and `BACKUP/public-pool.html` to
`/usr/share/nginx/html/x-post-material-pool.html`. Restart
`x-post-automation.service`, `x-auto-post-service.service` and
`drama-material-api.service`, verify their health, then run
`python3 SOURCE/ops/x-pool-drama-language-20261012/install.py --restore BACKUP`.
Never restore old databases, tokens, queues, publication facts or validation
receipts as part of code rollback.
