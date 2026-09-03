# TT Minis bid protection sync

This module imports TikTok Bid Protection daily history for every dynamically discovered TT Minis product into one table:

`ads_ai.ads_tiktok_minis_bid_protection_daily`

It does not provide a web page, an internal HTTP API, Feishu delivery, or currency conversion.

## Data contract

- Grain: `record_date + advertiser_id + data_level + query_id`.
- Levels: `CAMPAIGN` and `ADGROUP` are stored separately and must never be added together.
- Amount: `credit_amount_scaled` is the raw API integer; `credit_amount` is exactly the raw value divided by `100000`.
- Currency: kept as returned by TikTok; an empty value is allowed when TikTok returns no currency for zero credit.
- Product scope: starts from every positive-spend object for the day, then proves membership through `auto_created_data.publish_queue_id -> queue` using a numeric `product_id` and non-empty `minis_id`. `show_name` is only a saved snapshot and is never a filter. Current expected products are 3346, 3380, and 3416; later valid TT Minis products require no code change.
- Refresh: the previous day plus `UNDER_PROTECTION` and `CONFIRMING` rows still inside the 60-day API window.
- Sparse history: an omitted history ID is checked once through the status endpoint. Status-known omissions and failed API/DB batches go to the mode-0600 data-disk retry state; objects omitted by both successful endpoints are counted as `not_applicable` and do not form a dead retry queue. Successful facts are never deleted.

The DDL is in `001_create_ads_tiktok_minis_bid_protection_daily.sql`. Apply it once through the approved `ads_ai` write entry and validate it through the read-only entry before running the sync.

## Operations

Production runs on CPU host `43.166.187.96` from an immutable GitHub release under `/mnt/data-disk/tt-minis-bid-protection/releases/`. The `/mnt/data-disk/tt-minis-bid-protection/current` symlink points directly to the active release's `ops/tt-minis-bid-protection` module directory, so the cron can invoke `tt_minis_bid_protection_sync.py` from `current`. The normal database write entry is `101.32.56.53:63353`; readback uses `101.32.56.53:63350`.

The TikTok access token is read from `/root/codex_test/tt_business_api_tokens.sqlite3`, row `native_growth_default`. Never put the token in Git, command arguments, environment templates, logs, or test fixtures.

Rotate the shared token only through the guarded helper. It prompts without echo, freezes every advertiser where the old Token currently passes Native Growth, requires the new Token to pass Bid Protection status/history and Native Growth for that full baseline across all three current products, creates a consistent SQLite backup on the data disk, performs a full-row compare-and-swap update, and automatically restores only this change if post-write validation fails:

```bash
python3 rotate_tt_business_api_token.py --canary-date YYYY-MM-DD
```

Before enabling the schedule:

```bash
python3 -m py_compile ops/tt-minis-bid-protection/tt_minis_bid_protection_sync.py
python3 -m unittest discover -s ops/tt-minis-bid-protection -p 'test_*.py'
```

Run a read-only manual date first, then perform the initial 60-day backfill:

```bash
python3 tt_minis_bid_protection_sync.py --start-date 2026-09-02 --dry-run
python3 tt_minis_bid_protection_sync.py --backfill-days 60
```

Manual ranges use `--start-date YYYY-MM-DD` with optional `--end-date YYYY-MM-DD`. The normal root cron runs `--daily` once per day at `09:25 Asia/Shanghai` and uses its own `flock` lock and log. A partial API failure preserves successful upserts and exits with code `2` so operations do not confuse missing rows with zero compensation.

```cron
25 9 * * * /usr/bin/flock -xn /tmp/tt_minis_bid_protection_sync.lock -c "cd /mnt/data-disk/tt-minis-bid-protection/current && /usr/bin/python3 tt_minis_bid_protection_sync.py --daily" >> /mnt/data-disk/tt-minis-bid-protection/logs/tt_minis_bid_protection_sync.log 2>&1
```

## Query examples

Daily product compensation must select exactly one level and group by currency:

```sql
SELECT
  record_date,
  product_id,
  MAX(product_name) AS product_name,
  currency,
  SUM(credit_amount) AS credit_amount
FROM ads_ai.ads_tiktok_minis_bid_protection_daily
WHERE product_id = 3346
  AND record_date = '2026-09-02'
  AND data_level = 'CAMPAIGN'
GROUP BY record_date, product_id, currency;
```

Campaign detail:

```sql
SELECT
  advertiser_id,
  campaign_id,
  protection_status,
  status_detail,
  credit_amount,
  currency,
  sync_at
FROM ads_ai.ads_tiktok_minis_bid_protection_daily
WHERE product_id = 3346
  AND record_date = '2026-09-02'
  AND data_level = 'CAMPAIGN'
ORDER BY advertiser_id, campaign_id;
```

Pending records due for a refresh:

```sql
SELECT record_date, advertiser_id, data_level, query_id
FROM ads_ai.ads_tiktok_minis_bid_protection_daily
WHERE protection_status IN ('UNDER_PROTECTION', 'CONFIRMING')
  AND record_date >= CURRENT_DATE - INTERVAL 60 DAY
ORDER BY record_date, advertiser_id, data_level, query_id;
```

Table rows alone cannot distinguish a genuine zero from a failed account because this requirement intentionally has no run-audit table. Check the cron process result and redacted log before declaring a daily load complete.
Failed request candidates are retained in `/mnt/data-disk/tt-minis-bid-protection/state/failed_requests.json`; this is operational retry state, not a second business table.

## Rollback

Remove only this exact cron line, switch `current` back to the previous commit, and keep the fact table and imported facts. If token rollback is needed, restore only the `native_growth_default` row with a compare-and-swap transaction and re-run Bid Protection status/history plus Native Growth read-only canaries; never overwrite the entire SQLite database.
