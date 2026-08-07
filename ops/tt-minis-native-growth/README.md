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
nginx -t
```

## Memory and publish-safety contract

- The 60-day summary payload deliberately omits dictionaries and compact rows; `latest.json` already publishes `rows=[]` and `dicts={}`, while daily detail JSON keeps the existing dictionary-encoded schema.
- SQLite rows are converted directly from the cursor instead of retaining a second `fetchall()` result.
- Refresh inserts stream parameters into `executemany()` instead of creating another full two-day parameter matrix.
- Every run writes detail JSON under its own `data/<version>/<level>/<day>.json` directory. Only after every detail file succeeds are `latest.json` and `index.html` atomically replaced, so a failed run cannot mix old and new detail data.
- Unreferenced version files are removed only after the replacement manifest is committed and after a 24-hour grace period.
- A first full-range validation must publish to a shadow directory on `/mnt/data-disk`; do not use the public report directory for an unverified build.

After deployment, request `latest.json` and one detail URL from its `data_files[*][*].path` with an authorized session. The expected cache headers are respectively `private, no-store` and `private, max-age=900`.

## Deployment and rollback

Back up both live files, install the generator and nginx location from this directory, run the validation above, publish once from the local SQLite cache, and reload nginx narrowly. The 30-minute cron remains responsible for source refresh and publishing.

Rollback restores the two timestamped backups, runs `nginx -t`, reloads nginx, and republishes with the restored generator. No SQLite data or generated report data should be deleted during rollback.
