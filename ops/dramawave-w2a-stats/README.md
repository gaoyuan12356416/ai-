# Account-pool statistics service

Independent loopback HTTP service on port 8850, hourly systemd refresh, and an exact Nginx route. No main-backend imports, restarts, business-table writes, or publishing scheduler changes.

## Source and persistence

- V1.5 delivery source: `kunlunads_dev.ads_tiktok_insights`, Campaign level `category=0`, `start_date >= '2026-11-01'`, exact `advertiser_id` in the configured pool. Impression/click/cost are `SUM(show_cnt)`, `SUM(click_cnt)`, `SUM(stat_cost)`. Do not add category 1/2 totals: they represent the same delivery at other reporting levels.
- Installation and revenue remain `SUM(installs)` and `SUM(af_revenue0)` from `kunlunads_dev.ads_custom_source_insight`, `dt >= '2026-11-01'`, exact `ad_account_id` in the same pool. All products and pages remain included; the historical `dramawave/2334` URL segments do not filter either source. Source dates and currencies are not converted.
- Each source aggregates by day independently, then metric-disjoint `UNION ALL` facts are summed by day in one SELECT/read-only transaction. There is no raw-fact JOIN, account intersection, positive-spend filter, or revenue loss for accounts without a delivery row. A day present in only one source retains its available metrics. A failed SELECT cannot publish partial results.
- Configured pool: `648`, verified in `ads_alternative_account_group`; live memberships are non-deleted `ads_alternative_accounts` records. An existing empty pool is successful empty data; a missing/unreadable pool is unavailable.
- Source adapter uses `/usr/local/bin/mysql-gated` on CPU, fixed read-only endpoint `101.32.56.53:63350`, explicit `@@read_only` validation, one connection at a time, bounded waits and SELECT execution. Never bypass the shared SQL gate for this automation.
- The two-source historical aggregate retains the 240-second statement budget; membership reads retain 20 seconds. Gate wait is 60 seconds, with one connection and no immediate retries. TikTok uses the advertiser/date `ads` index with an indexable text predicate plus a byte-exact predicate; custom metrics use `stats_date`. Integral DECIMAL count columns are parsed exactly without floating-point conversion or truncation.
- HTTP never queries MySQL. SQLite scope hash + snapshot transaction protects complete reads and scope changes. State is `/mnt/data-disk/dramawave-w2a-stats/state`; current release is `/opt/dramawave-w2a-stats/current`.
- The scope hash includes the report policy as well as pool membership. A snapshot from an older product/page/date policy is unavailable until the first successful current-policy refresh; no database rows or credentials are reset for this transition. Date requests entirely before 2026-11-01 return successful empty data once a current snapshot exists.
- `state_dir`, pool ID, token file, CIDRs and trusted proxies are read from `/etc/dramawave-w2a-stats.json` for each request/refresh. Secrets are server-only: `/etc/dramawave-w2a-stats.token` and `/etc/dramawave-w2a-stats.mysql.env`, mode 0600. The dedicated root refresh is required to enter the existing root-owned SQL gate; both services use systemd filesystem hardening.

## Checks

```bash
cd ops/dramawave-w2a-stats
python3 -m unittest -v test_w2a_stats test_source
python3 -m py_compile w2a_stats.py source.py deploy.py
```

Tests exercise actual HTTP and SQLite snapshots, two-source SELECT execution against relational fixtures, reporting-level and raw-join double-count prevention, delivery-only/revenue-only days, scope exclusions, exact AF D0 mapping, money/count precision, CIDR/XFF spoofing, state transitions, failed refresh preservation and source-gate credential handling.

## Deploy

1. Commit and push only these scoped files to GitHub. Fetch the exact SHA on CPU and archive `ops/dramawave-w2a-stats` into `/opt/dramawave-w2a-stats/releases/<SHA>`; retain `<SHA>` as release identity.
2. Run `/usr/bin/python3 /opt/dramawave-w2a-stats/releases/<SHA>/deploy.py install --release /opt/dramawave-w2a-stats/releases/<SHA>`.
3. The helper verifies the data-disk UUID, backs up only this endpoint's config/units and previous release target, provisions server-only secrets, validates Nginx, starts this service, performs the first refresh, enables the hourly timer and reloads Nginx. It does not restart the main API. Existing secret/config values are preserved on later installations.
4. Read `/healthz` over loopback, verify API access using the token internally without printing it, check external invalid-token rejection and spoofed XFF rejection, and confirm the timer's next trigger plus successful refresh exit code.
5. Deliver the API document and a separate local private token file to the user. Never put the token into GitHub, shell arguments, public documents, or logs.

For the V1.5 migration, first warm an isolated `state_dir` using a private copy of the existing server configuration and the exact staged release. Compare its daily totals with independent source aggregates. Save an online SQLite backup of the live snapshot before installing. The new policy hash makes a V1.4 custom-only snapshot unavailable under V1.5 until its first successful refresh. Every refresh rebuilds all available dates since November 1, including the current partial day, so the first successful activation also completes the requested November backfill. Read the authenticated public API after installation, and report only that successful snapshot. On rollback use the helper below to regenerate the previous source policy; do not expose V1.5 data under V1.4.

To activate an already verified candidate without a second long query, revalidate current pool membership against its policy/scope hash, call `deploy.backup(release)`, stop only the refresh timer, and let any active refresh finish. Under the live `refresh_lock`, take an online SQLite backup, stop only this dedicated HTTP service, copy the complete candidate using SQLite's backup API, atomically change `current`, and start this HTTP service. Verify V1.5 health and authenticated public data equality with the candidate, then restore the timer's prior active state and record `installed.json` in the backup directory. Units, Nginx routing, token/CIDRs and the main API remain unchanged. If activation fails, restore both the saved database and previous release while this endpoint is stopped. Subsequent standard rollback still uses the helper below.

On 2026-11-06, source metadata confirmed `af_revenue0 DECIMAL(20,2)` and `spend DECIMAL(10,2)`; pool 648 existed with no members, and W2A page 2334 had no source rows. These are dated observations, not permanent business constraints.

## Rollback

The installer prints its exact backup path before activation and records its result in `<backup>/installed.json`. Run:

```bash
/usr/bin/python3 /opt/dramawave-w2a-stats/releases/<SHA>/deploy.py rollback --backup /mnt/data-disk/dramawave-w2a-stats/backups/<timestamp>
```

Rollback waits only for this service's active refresh, restores the previous dedicated configs/units/release, and reloads Nginx. When restoring a running older release, it refreshes using that release before reopening HTTP so a broader snapshot cannot be returned under an older page-limited contract. On first installation it removes the new route/units. Credentials, source database and all other services remain intact; cached aggregates are recomputed, not restored from an old backup.
