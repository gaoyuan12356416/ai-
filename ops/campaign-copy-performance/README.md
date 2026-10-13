# Campaign Copy Performance Dynamic Report

This standalone read-only report is published at:

`https://ai.yingliangads.com/reports/campaign-copy-performance/`

The service reads Meta (`platform=0`) and TikTok (`platform=3`) Campaign, Ad Set, and Ad copy logs plus matched insight rows from the existing read-only MySQL endpoint. It refuses any configured port other than `63350` and verifies `@@read_only=1`.

The report supports the three `ads_campaign_rule_logs.level` values and exposes separate Campaign, Ad Set, and Ad tabs. Successful rows are matched to effects with these exact keys:

- `level=0`: `new_id = campaign_id`; sum the mapped Ads under that Campaign.
- `level=1`: `new_id = adset_id`; sum the mapped Ads under that Ad Set.
- `level=2`: `new_id = ad_id`; sum that exact Ad.

Meta structures are resolved through `ads_facebook_auto_created_data`; TikTok structures are resolved through `ads_tiktok_auto_created_data`. Both platforms use normalized effect rows from `ads_custom_source_insight`, filtered by the matching platform and exact mapped Ad IDs. Platform is part of every internal entity key so equal numeric IDs cannot mix across Meta and TikTok. The page exposes platform as a filter and pivot dimension, and exposes both AF D0 and IAA revenue/ROAS so TikTok Minis monetization is visible without combining distinct revenue definitions.

Each tab shows its own running/success/failed pipeline counts. Failed copies remain in the pipeline totals but cannot have post-copy effect rows because they have no successful `new_id`.

Data is refreshed by one background scheduler every 15 minutes. GET/HEAD requests only read the cache and never trigger SQL, including when the snapshot is stale. The last successful compact v2 payload is held in memory and atomically persisted at `/mnt/data-disk/campaign-copy-performance/cache/report.json`; a service restart can therefore serve the last cache immediately while a fresh query runs. Consecutive failures retry after 2, 4, 8, then at most 15 minutes and retain the last verified cache. Success restores the 15-minute start-to-start cadence. The page explicitly labels a stale snapshot; missing days must not be interpreted as zero activity.

The API pre-compresses the payload once, supports `ETag`/`304`, and permits private browser reuse for one minute. Its v2 wire format keeps the original Campaign arrays backward-compatible and adds compact Ad Set/Ad arrays plus per-level pipeline counts. The frontend starts fetching from `<head>`, shows an explicit cache-loading state, inflates the compact row format, and debounces text/number filters. Its statistics-date presets remain `全部`, `当天`, `昨天`, `近三天`, and `近七天`, based on the MySQL server date in timezone `+08:00`.

## Local checks

```bash
python3 -m py_compile service.py
python3 service.py --self-test
python3 test_contract.py
python3 test_mapping.py
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

## Refresh repair (2026-10-10)

The old full-history loop issued tens of thousands of small insight reads as the
report grew to about 250,000 Campaigns and 916,000 mapped Ads. Its last successful
refresh took 24,607 seconds; subsequent OperationalErrors left the October 8
snapshot visible. The source already contained October 9 and October 10 rows.

The refresh now reads all copy/mapping metadata, re-queries the latest seven days
(or every missing date after an outage), and revisits one older date each cycle.
The historical cursor advances only after a successful atomic publish. Newly
observed successful copies with old creation dates expand the backfill window.
Every refreshed date replaces old rows for that date, even when the new result is
empty. Other historical dates retain their previous values until their rotation;
no source database writes or changed revenue definitions are involved.

Insight batches contain 20,000 unique Ad IDs, mapping batches 3,000 IDs. The
production reader holds one FIFO SQL gate permit, checks read-only port/state,
uses a 15-second statement limit and 840-second overall database budget, and
closes its connection on gate loss. No refresh publishes partial results. Cold
builds or very long outages may exceed this budget and require a separately
supervised rebuild; existing historical cache must be preserved.

The date presets use the HTTP server clock in Beijing time even for a stale
snapshot. Editing either end of a reversed date range adjusts the other end.
Snapshots older than 30 minutes (or marked stale by the API) show an explicit
warning. Runtime logs include safe database error codes and per-date progress.

Use `python3 service.py --warm-cache` with a separate cache path to prepare a
candidate before switching a release. Copy the existing cache as its seed. The
same command can verify the next incremental refresh without taking down the
serving process. Deploy only this standalone report; no ad execution services,
cron entries, main backend, or Nginx changes are required.


### Production recovery evidence

- Host/path: `43.166.187.96:/opt/campaign-copy-performance/current`.
- Code release: `4727724c3cdafff5b5bbd2c727284f1ed953eca6` (fetched from GitHub).
- Previous release: `da7a2061c6e1eead34f890c7503354db05b33973`.
- Backup: `/mnt/data-disk/campaign-copy-performance/repair-20261010/backup/`.
- Candidate refresh: 408.829 seconds, source snapshot `2026-10-10 11:06:23 +08:00`;
  258,858 Campaigns, 5,120 Ad Set/Ad entities, 645,909 daily rows.
- October 9 Campaigns: 11,569 daily rows, spend 251,933.61, AF D0 revenue 96,287.39.
  October 10 (partial day): 5,025 rows, spend 56,437.94, AF D0 revenue 17,705.21.
- No duplicate entity/date keys; 551,598 Campaign-day and 15,213 Ad Set/Ad-day rows
  outside the refresh dates were exactly retained. Nine independent read-only
  source samples matched spend, AF D0 revenue, IAA revenue and AF installs.
- Ad Set had no source insight rows on October 9/10: all 2,719 copied Meta Ad Sets
  and their 4,921 mapped Ads were checked. There were no copied TikTok Ad Sets.
- 15 local/server tests passed; JavaScript syntax passed. Browser fixture checks
  verified Beijing yesterday/today, both reversed-date edits and stale warnings.
- Only `campaign-copy-performance.service` restarted at `2026-10-10 11:14:28 +08:00`.
  Listener ready after 14 seconds; loopback and public compressed API HEAD returned
  200 and ETag matching cache SHA-256
  `b9b88bbeaede1787ee25bf593129ac955e30fabe2bb4aee95f8882f9a7bbd06e`.
  No Nginx/main-backend/advertising execution restart or configuration change.
- Safe detailed evidence remains under the repair directory on the data disk.

Rollback the code while retaining the recovered, backward-compatible v2 cache:

```bash
set -e
ln -s /opt/campaign-copy-performance/releases/da7a2061c6e1eead34f890c7503354db05b33973/ops/campaign-copy-performance /opt/campaign-copy-performance/current.rollback
mv -Tf /opt/campaign-copy-performance/current.rollback /opt/campaign-copy-performance/current
systemctl restart campaign-copy-performance.service
```

Restoring the old code also restores the slow full-history refresh, so this is an
emergency rollback only. Do not overwrite the repaired cache with the old October 8
snapshot during a code rollback. No unit or Nginx rollback is necessary.


The subsequent production background cycle also succeeded at `2026-10-10
11:23:21 +08:00`, taking 522.605 seconds and moving the source snapshot to
`11:14:43` with 258,872 Campaigns. The historical cursor advanced to July 19,
confirming successful incremental continuity. Start-to-start scheduling remains
15 minutes.

A server-side Chromium visit to the real public URL fully rendered in 26.658
seconds; yesterday/today filters showed the expected 11,569 / 5,025 Campaign-day
rows and 251,933.61 / 56,437.94 spend from its 11:06 snapshot, with zero browser
console errors. The Windows verification network was unusually slow and reset a
large download, so that local failure is not counted as a successful public UI
check. Full report transport remains about 23.5 MB compressed; cold loads depend
on the client connection speed.

## Query optimization (2026-10-13)

The previous refresh repeatedly fetched all Campaign descriptions (about 1.01M
mapping rows for 282K Campaigns). `campaign_mapping.py` now reads **exact current
Campaign/Ad membership** through covering indexes, in batches of 5,000: TikTok
`campaign_id_2 (campaign_id, adset_id, ad_id)` and Meta `campaign_id` with the same
columns. Meta fallback remains per Campaign: use `ads_ai` only when `kunlunads_dev`
has no rows. No database DDL or source writes are required.

Only new Campaigns, changed membership, and a rotating 1/96 of existing Campaigns
fetch descriptions, with at most 250 IDs per detailed query. Campaign names,
countries, languages, and the source product fallback have a maximum successful
refresh age of 24 hours. Copy-log App names and financial metrics still refresh
every cycle. `status`, `budget`, and `ad_account_id` were not used by this report
and are no longer fetched for Campaign mappings. Ad Set/Ad queries are unchanged.

Membership changes (including equal-count Ad replacement, deletion, duplicate
rows, or fallback-source changes) are detected with a compact signature. The
signature controls descriptive-field reuse only: every metric query uses exact
freshly read Ad IDs, never a cached membership set or digest. A failed refresh
does not advance metadata ages, rotation, financial dates, or history cursors.
The durable v2 cache adds optional metadata fields to `cf/c`; the old service can
still read it. These fields and the metadata cursor are stripped from the public
API, avoiding extra browser transfer and keeping the original frontend contract.
The ETag hashes the public body, not the private seed file. The first migration reuses verified descriptors when
Ad counts match. A missing Meta source-product fallback is fetched immediately
only when the current log lacks an App name; otherwise it stays explicitly
unknown until that Campaign's normal metadata rotation. This avoids treating an
old resolved App label as the raw product or forcing a full migration lookup.

Copy pipeline counts and successful logs now come from one logical log scan,
removing the second full scan and SQL filesort. The scan is split into primary-key
pages of at most 500,000 source rows to stay below the statement timeout. An
index-only seek finds each page boundary, including across sparse/deleted IDs;
old status corrections remain visible because the full source population is
still covered. All statements in one refresh use a
read-only, repeatable-read consistent snapshot under the existing FIFO gate.
Phase timings/query counts and mapping reuse counts are logged without SQL text,
IDs, or credentials. The existing statement/overall budgets and atomic publish
remain in force.

Validation: seed a separate candidate cache from the currently published cache,
run `service.py --warm-cache` under the gate, then run:

```bash
python3 validate_cache.py /path/to/seed.json /path/to/candidate.json
python3 -m unittest discover -p 'test_*.py'
```

Validate a second warm refresh to measure normal reuse after migration. Verify
real source samples and public HTTP/ETag after switching only this standalone
service. A warm operation never writes to the live cache path. Roll back to
release `4727724c3cdafff5b5bbd2c727284f1ed953eca6`, preserving the latest v2 cache:

```bash
ln -s /opt/campaign-copy-performance/releases/4727724c3cdafff5b5bbd2c727284f1ed953eca6/ops/campaign-copy-performance /opt/campaign-copy-performance/current.rollback
mv -Tf /opt/campaign-copy-performance/current.rollback /opt/campaign-copy-performance/current
systemctl restart campaign-copy-performance.service
```
