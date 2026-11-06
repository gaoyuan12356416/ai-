# DramaWave account-pool statistics service

Independent loopback HTTP service on port 8850, hourly systemd refresh, and an exact Nginx route. No main-backend imports, restarts, business-table writes, or publishing scheduler changes.

## Source and persistence

- Fixed source: `kunlunads_dev.ads_custom_source_insight`, product `Dramawave`, all page IDs, `revenue = SUM(af_revenue0)`. V1.2 removes the page predicate and its forced page index; the historical URL remains compatible, including its `2334` segment.
- Configured pool: `648`, verified in `ads_alternative_account_group`; live memberships are non-deleted `ads_alternative_accounts` records. An existing empty pool is successful empty data; a missing/unreadable pool is unavailable.
- Source adapter uses `/usr/local/bin/mysql-gated` on CPU, fixed read-only endpoint `101.32.56.53:63350`, explicit `@@read_only` validation, one connection at a time, bounded waits and SELECT execution. Never bypass the shared SQL gate for this automation.
- The all-page historical aggregate has a 240-second statement budget; membership reads retain 20 seconds. Gate wait is 60 seconds, with one connection and no immediate retries. This replaces the old page-query time budget, which timed out with the now-populated 209-account pool.
- HTTP never queries MySQL. SQLite scope hash + snapshot transaction protects complete reads and scope changes. State is `/mnt/data-disk/dramawave-w2a-stats/state`; current release is `/opt/dramawave-w2a-stats/current`.
- V1.2 hashes the report policy as well as pool membership. A V1.1 page-only snapshot is unavailable until the first successful all-pages refresh; no database rows or credentials are reset for this transition.
- `state_dir`, pool ID, token file, CIDRs and trusted proxies are read from `/etc/dramawave-w2a-stats.json` for each request/refresh. Secrets are server-only: `/etc/dramawave-w2a-stats.token` and `/etc/dramawave-w2a-stats.mysql.env`, mode 0600. The dedicated root refresh is required to enter the existing root-owned SQL gate; both services use systemd filesystem hardening.

## Checks

```bash
cd ops/dramawave-w2a-stats
python3 -m unittest -v test_w2a_stats test_source
python3 -m py_compile w2a_stats.py source.py deploy.py
```

Tests exercise actual HTTP and SQLite snapshots, source SELECT execution against relational fixtures, scope exclusions, exact AF D0 mapping, money precision, CIDR/XFF spoofing, state transitions, failed refresh preservation and source-gate credential handling.

## Deploy

1. Commit and push only these scoped files to GitHub. Fetch the exact SHA on CPU and archive `ops/dramawave-w2a-stats` into `/opt/dramawave-w2a-stats/releases/<SHA>`; retain `<SHA>` as release identity.
2. Run `/usr/bin/python3 /opt/dramawave-w2a-stats/releases/<SHA>/deploy.py install --release /opt/dramawave-w2a-stats/releases/<SHA>`.
3. The helper verifies the data-disk UUID, backs up only this endpoint's config/units and previous release target, provisions server-only secrets, validates Nginx, starts this service, performs the first refresh, enables the hourly timer and reloads Nginx. It does not restart the main API. Existing secret/config values are preserved on later installations.
4. Read `/healthz` over loopback, verify API access using the token internally without printing it, check external invalid-token rejection and spoofed XFF rejection, and confirm the timer's next trigger plus successful refresh exit code.
5. Deliver the API document and a separate local private token file to the user. Never put the token into GitHub, shell arguments, public documents, or logs.

On 2026-11-06, source metadata confirmed `af_revenue0 DECIMAL(20,2)` and `spend DECIMAL(10,2)`; pool 648 existed with no members, and W2A page 2334 had no source rows. These are dated observations, not permanent business constraints.

## Rollback

The installer prints its exact backup path before activation and records its result in `<backup>/installed.json`. Run:

```bash
/usr/bin/python3 /opt/dramawave-w2a-stats/releases/<SHA>/deploy.py rollback --backup /mnt/data-disk/dramawave-w2a-stats/backups/<timestamp>
```

Rollback waits only for this service's active refresh, restores the previous dedicated configs/units/release, and reloads Nginx. When restoring a running older release, it refreshes using that release before reopening HTTP so a broader snapshot cannot be returned under an older page-limited contract. On first installation it removes the new route/units. Credentials, source database and all other services remain intact; cached aggregates are recomputed, not restored from an old backup.
