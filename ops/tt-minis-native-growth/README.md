# TT Minis Native Growth report

This directory is the GitHub-maintained source for the standalone report at:

- `https://ai.yingliangads.com/reports/tt-minis-native-growth/`
- runtime generator: `/root/codex_test/tt_minis_multi_dim_dashboard.py`
- nginx location: `/etc/nginx/default.d/tt-minis-native-growth-auth.conf`
- published files: `/usr/share/nginx/html/reports/tt-minis-native-growth/`

## Browser cache contract

- `index.html` and `latest.json` stay `private, no-store`. Every page open reads the current manifest and therefore sees the newest `generated_at` version.
- Daily detail files under `data/` are private-browser cached for 15 minutes (`max-age=900`). The page also stores the authenticated, versioned JSON in same-origin Cache Storage for 15 minutes, so reopening still avoids the server when browser network caching is disabled.
- Detail URLs include `?v=<generated_at>`. A successful scheduled publish changes the version immediately, so a fresh manifest does not reuse detail files from the previous publish.
- Cache Storage entries carry a local timestamp and are deleted after the 15-minute TTL. Network and Cache Storage keys both include `generated_at`.
- The cache is browser-private. Shared proxies and CDNs must not store authenticated report data.

## Validation

```bash
python3 -m py_compile ops/tt-minis-native-growth/tt_minis_multi_dim_dashboard.py
python3 ops/tt-minis-native-growth/test_browser_cache_contract.py
python3 -m unittest ops/tt-minis-native-growth/test_memory_safety_contract.py
python3 -m unittest ops/tt-minis-native-growth/test_source_query_optimization.py
nginx -t
```

## Source refresh query contract

- Refresh first aggregates only the requested dates from `ads_tiktok_insights FORCE INDEX(pcsa)`.
- It extracts the resulting ad or campaign IDs, normalizes them as digit-only strings, and loads publish metadata in bounded batches of 5,000 through the `ads_tiktok_auto_created_data.ad_id` or `.campaign_id` index. Each batch selects the exact latest created-data primary key per metric ID with `MAX(created_at)` followed by the tie-breaking `MAX(id)`, then reads the full row back through `PRIMARY`; it must not restore ordered `GROUP_CONCAT` field scans. At 19-digit TikTok IDs this keeps each SQL argument comfortably below the host process limit while amortizing per-query latency.
- The indexed string lookup relies on canonical TikTok IDs. Before deployment, verify the target insight dates have no whitespace/non-canonical or leading-zero IDs. The read-only 2026-08-09 through 2026-08-10 check covered 272,167 ad insight rows and 133,193 campaign insight rows; both mismatch and leading-zero counts were zero.
- Each metadata row still joins `tiktok_publish_template_queue` by its primary key and requires the exact Dramawave minis ID. Python then performs the inner-join-equivalent merge, so out-of-scope insight rows remain excluded.
- Install enrichment scans each requested compact `dt` once with `ads_app_revenues FORCE INDEX(dt)`, aggregates by campaign or ad ID, and keeps only the scoped insight keys in Python. Do not restore 500-ID key-first chunks; at current campaign volume, the repeated chunks became a multi-minute bottleneck.
- Do not restore the former query that grouped the complete minis queue/created-data history before joining the requested insight dates.
- MySQL credentials are removed from the child-process argv and passed through `MYSQL_PWD`. Timeout and query failures expose only the error type, return code, and at most 400 characters of redacted stderr; SQL and command arguments are not logged.
- The read-only 2026-08-09 production shadow completed campaign source refresh in 93.175 seconds (9.929 insight, 63.792 metadata, 3.254 content, 15.328 install enrichment) and ad source refresh in 131.118 seconds (12.384, 99.365, 3.377, 14.219). Against the just-refreshed legacy cache, both levels had identical row counts, spend, installs, impressions, clicks, and every non-metric output field; revenue and ad-impression had only expected live-source drift between query times. Exact latest-scope rows had zero nulls in user/account/campaign/adset/created-at fields across 24,581 campaign and 44,590 ad IDs.
- On that shadow, `ads_app_revenues.dt` was `char(10)` with a BTREE `dt` index; the legacy two-day `BETWEEN` range covered 361,385 rows with zero null or non-eight-digit values. `EXPLAIN` selected `dt` as `ref`, and actual per-day aggregates completed in 15.328 seconds for campaign and 14.219 seconds for ad.

## Memory and publish-safety contract

- The 60-day summary is accumulated directly from the SQLite cursor and never materializes the full campaign row set. `latest.json` still publishes `rows=[]` and `dicts={}`, while daily detail JSON keeps the existing dictionary-encoded schema.
- SQLite rows are converted directly from the cursor instead of retaining a second `fetchall()` result.
- Refresh inserts stream parameters into `executemany()` instead of creating another full two-day parameter matrix.
- Every run writes detail JSON under its own `data/<version>/<level>/<day>.json` directory. Only after every detail file succeeds are `latest.json` and `index.html` atomically replaced, so a failed run cannot mix old and new detail data.
- Unreferenced version files are removed only after the replacement manifest is committed and after a 24-hour grace period.
- A first full-range validation must publish to a shadow directory on `/mnt/data-disk`; do not use the public report directory for an unverified build.

After deployment, request `latest.json` and one detail URL from its `data_files[*][*].path` with an authorized session. The expected cache headers are respectively `private, no-store` and `private, max-age=900`.

## Deployment and rollback

Back up both live files, install the generator and nginx location from this directory, run the validation above, publish once from the local SQLite cache, and reload nginx narrowly. The 30-minute cron remains responsible for source refresh and publishing.

Rollback restores the two timestamped backups, runs `nginx -t`, reloads nginx, and republishes with the restored generator. No SQLite data or generated report data should be deleted during rollback.
