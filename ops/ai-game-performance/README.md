# AI Game Performance report

GitHub-maintained source for the standalone, Feishu-protected report at:

- `https://ai.yingliangads.com/reports/ai-game-performance/`
- release root: `/opt/ai-game-performance/releases/<commit>`
- current symlink: `/opt/ai-game-performance/current`
- SQLite/cache root: `/mnt/data-disk/ai-game-performance`
- public static root: `/usr/share/nginx/html/reports/ai-game-performance`

## Frozen data contract

- Product conversion fact: read-only `ads_ai.ads_manual_daily_performance`.
- Delivery fact: read-only `kunlunads_dev.ads_custom_source_insight`, exact `product='Neonarcade'` and platforms `0/1/3`.
- Google maps by `campaign_id + adset_id`; Meta/TikTok map by `ad_id`.
- Only a one-game mapping is assigned. Ambiguous and unmapped delivery rows remain separate.
- Unity has no unified delivery rows in the current source and uses `manual cost` only as an explicitly labelled overview fallback.
- The channel view loads the versioned delivery and conversion day files together, appends them as parallel facts, and aggregates only after the operator selects dimensions. It never raw-joins the two fact tables; Google/Meta/TikTok effective spend still comes only from delivery, Unity from manual fallback, and organic/restricted remain zero-spend conversion channels.
- Delivery `country` and conversion `country` remain separate dimensions.
- Live `play_duration_seconds` is total play time. Average play time is `SUM(total seconds) / SUM(installs)`; the legacy average column is converted to total seconds on read.
- Browser requests never query MySQL.

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
python3 /opt/ai-game-performance/current/ops/ai-game-performance/ai_game_performance_dashboard.py \
  --full-refresh --publish \
  --output-dir /mnt/data-disk/ai-game-performance/shadow/public

python3 /opt/ai-game-performance/current/ops/ai-game-performance/ai_game_performance_dashboard.py \
  --refresh-cache --publish
```

The production generator explicitly prepends `AI_GAME_REPORT_BASE_MODULE_DIR` (default `/root/codex_test`) and lazily imports `opera_product_daily_dashboard.py` for the established read-only MySQL command. Secrets stay server-local and are removed from child-process arguments.
