# TT Minis Native Growth report

This directory is the GitHub-maintained source for the standalone report at:

- `https://ai.yingliangads.com/reports/tt-minis-native-growth/`
- runtime generator: `/root/codex_test/tt_minis_multi_dim_dashboard.py`
- nginx location: `/etc/nginx/default.d/tt-minis-native-growth-auth.conf`
- published files: `/usr/share/nginx/html/reports/tt-minis-native-growth/`

## Browser cache contract

- `index.html` and `latest.json` stay `private, no-store`. Every page open reads the current manifest and therefore sees the newest `generated_at` version.
- Daily detail files under `data/` are private-browser cached for 15 minutes (`max-age=900`).
- Detail URLs include `?v=<generated_at>`. A successful scheduled publish changes the version immediately, so a fresh manifest does not reuse detail files from the previous publish.
- The cache is browser-private. Shared proxies and CDNs must not store authenticated report data.

## Validation

```bash
python3 -m py_compile ops/tt-minis-native-growth/tt_minis_multi_dim_dashboard.py
python3 ops/tt-minis-native-growth/test_browser_cache_contract.py
nginx -t
```

After deployment, request `latest.json` and one versioned `data/YYYY-MM-DD.json` with an authorized session. The expected cache headers are respectively `private, no-store` and `private, max-age=900`.

## Deployment and rollback

Back up both live files, install the generator and nginx location from this directory, run the validation above, publish once from the local SQLite cache, and reload nginx narrowly. The 30-minute cron remains responsible for source refresh and publishing.

Rollback restores the two timestamped backups, runs `nginx -t`, reloads nginx, and republishes with the restored generator. No SQLite data or generated report data should be deleted during rollback.
