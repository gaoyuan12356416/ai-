# Campaign Copy Performance Dynamic Report

This standalone read-only report is published at:

`https://ai.yingliangads.com/reports/campaign-copy-performance/`

The service reads Meta (`platform=0`) and TikTok (`platform=3`) Campaign, Ad Set, and Ad copy logs plus matched insight rows from the existing read-only MySQL endpoint. It refuses any configured port other than `63350` and verifies `@@read_only=1`.

The report supports the three `ads_campaign_rule_logs.level` values and exposes separate Campaign, Ad Set, and Ad tabs. Successful rows are matched to effects with these exact keys:

- `level=0`: `new_id = campaign_id`; sum the mapped Ads under that Campaign.
- `level=1`: `new_id = adset_id`; sum the mapped Ads under that Ad Set.
- `level=2`: `new_id = ad_id`; sum that exact Ad.

Meta structures are resolved through `ads_facebook_auto_created_data`; TikTok structures are resolved through `ads_tiktok_auto_created_data`. Both platforms use normalized effect rows from `ads_custom_source_insight`, filtered by the matching platform and exact mapped Ad IDs. Platform is part of every internal entity key so equal numeric IDs cannot mix across Meta and TikTok. The page exposes platform as a filter and pivot dimension.

Each tab shows its own running/success/failed pipeline counts. Failed copies remain in the pipeline totals but cannot have post-copy effect rows because they have no successful `new_id`.

Data is refreshed in a background thread every 15 minutes, so a page request never owns the database refresh. The last successful compact v2 payload is held in memory and atomically persisted at `/mnt/data-disk/campaign-copy-performance/cache/report.json`; a service restart can therefore serve the last cache immediately while a fresh query runs. Failed refreshes retry after two minutes and keep the most recent valid cache for up to 24 hours.

The API pre-compresses the payload once, supports `ETag`/`304`, and permits private browser reuse for one minute. Its v2 wire format keeps the original Campaign arrays backward-compatible and adds compact Ad Set/Ad arrays plus per-level pipeline counts. The frontend starts fetching from `<head>`, shows an explicit cache-loading state, inflates the compact row format, and debounces text/number filters. Its statistics-date presets remain `全部`, `当天`, `昨天`, `近三天`, and `近七天`, based on the MySQL server date in timezone `+08:00`.

## Local checks

```bash
python3 -m py_compile service.py
python3 service.py --self-test
python3 test_contract.py
node --check < extracted-inline-script.js
```

## Production files

- release directory: `/opt/campaign-copy-performance/releases/<commit>/`
- current symlink: `/opt/campaign-copy-performance/current`
- systemd unit: `/etc/systemd/system/campaign-copy-performance.service`
- Nginx include: `/etc/nginx/default.d/campaign-copy-performance.conf`
- loopback listener: `127.0.0.1:8831`
- persistent cache: `/mnt/data-disk/campaign-copy-performance/cache/report.json`

Deploy by checking out the exact GitHub commit into a new release directory, copying only this directory into that release, validating the self-test, confirming the secondary data disk is mounted and writable, creating the cache directory, switching the `current` symlink, and restarting only `campaign-copy-performance.service`. Validate `nginx -t` before reloading Nginx.

Rollback by switching `/opt/campaign-copy-performance/current` to the previous release, restoring the timestamped unit/Nginx backups, then running:

```bash
systemctl daemon-reload
systemctl restart campaign-copy-performance.service
nginx -t && systemctl reload nginx
```
