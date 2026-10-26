# One-time violating Facebook Page Post cleanup

Authorized scope: current members of Page groups **3, 4, 24, 25**, with at least
one `ads_facebook_page_ins.status=0` record; published in the frozen last 90 days;
`ads_source.source_type=3` and current `fb_violation=1`. No scheduler or public API.

Ordinary `source_id` is `ads_source.id`; that row's `source_id` is the custom
material ID. `ads_post_auto_publish_logs.resource_id` must agree when populated.
AI `graph_post_id` from video publishing is a video ID, requiring Graph linkage.
Explicit Post IDs still receive a Page-owner/time check before deletion.

## Run

Use Python 3.9+, requests, pymysql on CPU 43.166.187.96. Credentials are read from
the deployed dotenv files and remain process-local. Source MySQL is pinned to
the read-only 63350 endpoint; publisher SQLite opens in URI `mode=ro`.

```bash
python3 cleanup.py scan --run-dir /mnt/data-disk/fb-violation-post-cleanup/runs/RUN
python3 cleanup.py execute --stage canary --run-dir /mnt/data-disk/fb-violation-post-cleanup/runs/RUN --manifest-sha256 SHA256
python3 cleanup.py execute --stage remaining --run-dir /mnt/data-disk/fb-violation-post-cleanup/runs/RUN --manifest-sha256 SHA256
python3 cleanup.py reconcile --run-dir /mnt/data-disk/fb-violation-post-cleanup/runs/RUN --manifest-sha256 SHA256
python3 cleanup.py report --run-dir /mnt/data-disk/fb-violation-post-cleanup/runs/RUN --manifest-sha256 SHA256
```

The manifest is immutable and bound to one FULL-synchronous SQLite ledger.
An independent process lock prevents concurrent invocations. A global execution
binding also refuses a fresh ledger, so a rescan cannot bypass unknown writes.
The first five actual deletions must have explicit API success plus a subsequent
unavailable Post with a still-readable matching Page identity. Remaining work
uses at most two workers, one worker per Page, with shared Graph pacing/cooldown.
Current scope/material flags are checked again at the request boundary.

Never replay a DELETE on timeout, connection loss, or process interruption.
Recovery changes in-flight intents to `unknown`; `reconcile` only sends GETs and
never turns mere inaccessibility into an acknowledged deletion. Permission errors
and unreadable posts are separate from successfully deleted posts. Source logs,
publisher queues, ads, and account video libraries are never changed.

`manifest.json`, `source-snapshot.json`, `deletions.sqlite3`, `results.json`, and
`results.csv` preserve scope, provenance, individual outcomes and group totals.
Cross-group totals overlap. Tokens are excluded from artifacts and errors.

## Verification and stopping

```bash
python3 -m unittest discover -s tests -v
```

Deploy the exact GitHub commit into a separate data-disk release directory; do
not restart existing publishers. Stop only this worker PID to prevent further
requests, retain the current ledger, and run GET-only reconciliation. Already
deleted Posts cannot be recovered by restoring local files or an older ledger.
