# AI Game Performance report

GitHub-maintained source for the standalone, Feishu-protected report at:

- `https://ai.yingliangads.com/reports/ai-game-performance/`
- release root: `/opt/ai-game-performance/releases/<commit>`
- current symlink: `/opt/ai-game-performance/current`
- SQLite/cache root: `/mnt/data-disk/ai-game-performance`
- public static root: `/usr/share/nginx/html/reports/ai-game-performance`

## Frozen data contract

- Product conversion fact: read-only `ads_ai.ads_manual_daily_performance`.
- Delivery facts are read independently from read-only MySQL and appended in SQLite: `kunlunads_dev.ads_custom_source_insight` with exact `product='Neonarcade'` and platforms `0/1/3`, plus `kunlunads_dev.ads_unity_insights` with exact `product='Neonarcade'` and `category=0` through `FORCE INDEX(idx_date)`.
- Google maps by `campaign_id + adset_id`; Meta/TikTok map by `ad_id`; Unity maps only within the same date by `campaign_id + creative_pack_id/ad_id`.
- Only a one-game mapping is assigned. Ambiguous and unmapped delivery rows remain separate.
- Unity maps `starts` (Unity's impressions metric) to `source_impressions`, `clicks` to `source_clicks`, and `installs` to `source_installs`; `views` is completed views and is not exposed as impressions. `category=1` is excluded because it duplicates category-0 installs/spend at a different grain.
- Unity source ids use the negative namespace (`-ads_unity_insights.id`) so they cannot collide with positive `ads_custom_source_insight.id`. Strict `projectid[digits]` extraction from `creative_pack_name` is persisted as additive `delivery_fact.source_game_id`; older caches are migrated in place.
- The channel view loads the versioned delivery and conversion day files together, appends them as parallel facts, and aggregates only after the operator selects dimensions. It never raw-joins metric rows; Google/Meta/TikTok effective spend comes only from delivery, Unity effective spend remains the manual-cost fallback, Unity source spend stays zero, and organic/restricted remain zero-spend conversion channels.
- Channel CPI keeps the existing `source_spend / source_installs` contract. Unity source spend stays zero, so Unity source CPI is zero; the separately labelled manual-cost fallback affects effective spend only.
- Delivery `country` and conversion `country` remain separate dimensions.
- Live `play_duration_seconds` is total play time. Average play time is `SUM(total seconds) / SUM(installs)`; the legacy average column is converted to total seconds on read.
- Cards and tables display average play time as `seconds / 60` with the `min` unit. CSV exports the same minute value under `平均游戏时长(min)`; cached JSON and SQLite values remain seconds.
- Browser requests never query MySQL.

The Unity additions above were released on 2026-08-26 from runtime commit `28cefbb0c6439bea53b243de2595e789002dfa64`. Production shadow reconciliation, backup, full refresh, browser QA, and a natural timer refresh all passed; the verified natural-refresh data version is `20260826T174301241241+0800`.

## Cache and publish contract

- Refresh reads one date at a time to bound memory, replaces that fact/date in a SQLite transaction, then remaps cached delivery facts locally.
- `latest.json` is the publish commit point. Every data file is first written under `data/<version>/<view>/<day>.json`.
- `latest.json` and `index.html` are `private, no-store`; versioned daily files are `private, max-age=900` and use same-origin Cache Storage for 15 minutes.
- A failed refresh or publish does not replace `latest.json`.
- Retention is 60 days. Normal runs refresh Beijing today and the previous two days so D1 retention backfills are captured, while the first run uses `--full-refresh`.
- Production timer runs every 30 minutes at `*:12` and `*:42`, with up to 30 seconds randomized delay; a normal refresh republishes after the three-day incremental read completes.

## Local validation

```powershell
python -m py_compile ops\ai-game-performance\ai_game_performance_dashboard.py
python -m unittest discover -s ops\ai-game-performance -p "test_*.py" -v
python ops\ai-game-performance\validate_frontend_contract.py
git diff --check
```

## Production commands

```bash
UNITY_V7_RELEASE=/opt/ai-game-performance/releases/REPLACE_WITH_EXACT_GITHUB_SHA

python3 "${UNITY_V7_RELEASE}/ops/ai-game-performance/ai_game_performance_dashboard.py" \
  --full-refresh --publish \
  --cache-db /mnt/data-disk/ai-game-performance/shadow/v7/cache/ai-game-performance.sqlite3 \
  --output-dir /mnt/data-disk/ai-game-performance/shadow/v7/public

# Run only after current points to the same reviewed v7 release.
python3 /opt/ai-game-performance/current/ops/ai-game-performance/ai_game_performance_dashboard.py \
  --full-refresh --publish
```

The shadow command must always use both an isolated `--cache-db` and an isolated `--output-dir`; it must never migrate or replace the live cache. After shadow reconciliation and the live SQLite online backup, production repeats the full refresh against the live cache under the shared service lock. A code rollback after the v7 refresh restores the pre-v7 SQLite backup as well as the prior release and public commit point.

The production generator explicitly prepends `AI_GAME_REPORT_BASE_MODULE_DIR` (default `/root/codex_test`) and lazily imports `opera_product_daily_dashboard.py` for the established read-only MySQL command. Secrets stay server-local and are removed from child-process arguments.
