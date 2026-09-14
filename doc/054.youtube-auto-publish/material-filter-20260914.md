# YouTube material filter update — 2026-09-14

The material pool now selects `kunlunads_dev.ads_custom_source` rows using only
`product='Drama-社媒专用素材' AND category='dramawave_post'`.
This supersedes the five temporary conditions recorded in material-sql-activation.md:
data_source, task_id, product, designer, and created_at. No date restriction is retained.
The category uses equality; the underscore is a literal character.

Source: `deploy/youtube-auto-material-source.sql`.
Production: CPU `43.166.187.96:/etc/youtube-auto-publish/material-source.sql`.
The existing explicit projection, HTTPS normalization, drama association, app ID,
SQL gate, replica, timeouts, and 100-row result limit remain in effect.
SQL text is part of the cache key, so replacement takes effect on the next request
without restarting API or publishing workers. Existing frozen tasks are not rewritten.

Pre-deployment verification used the deployed MaterialSource and DramaMetadataResolver
against the read-only replica: 6 results, all 6 link_ready and drama_status=matched;
5.235 seconds. ID search returned 1 row in 4.187 seconds. Local validate_sql and
git diff --check passed. No publish, generation, comment, or notification was triggered.

Deployment procedure: fetch the exact GitHub commit into the existing data-disk bare
repository, extract only this SQL, verify the expected old SHA, back up the old file
and metadata to `/mnt/data-disk/deploy/youtube-auto-publish/backups`, then atomically
replace the config with mode 0600. Re-read through the production material adapter
and check service PIDs. Do not deploy the rest of this branch as an application release.

Rollback restores only the SQL from the deployment backup to the configuration path;
no database or asset restoration and no service restart are required. Exact production
backup and post-deployment results are appended after verification.

## Production verification

Deployed SQL commit: `738819e27b59e097a10157ea3e2a33d22b474cf8` (pushed to GitHub,
then fetched and verified on CPU). Installed SQL SHA256:
`2c37b57fc45e2f01baee1fe87bcdc87a932dacad62c9a3ad2cc7f347bb9ae356`.
Backup: `/mnt/data-disk/deploy/youtube-auto-publish/backups/material-filter-20260914-164744`.

After replacement, the deployed adapter read the configured path successfully:
6 materials, all 6 link_ready, list in 4.768 seconds; exact get-by-ID passed.
Material IDs: 6667528, 6667657, 6667658, 6667662, 6667663, 6667803.
All four API/publishing services remained active with unchanged PIDs.
Evidence: `manifest.json` and `verification.json` in the backup directory.

Exact SQL-only rollback:

```bash
cp -p /mnt/data-disk/deploy/youtube-auto-publish/backups/material-filter-20260914-164744/material-source.sql /etc/youtube-auto-publish/material-source.sql.rollback
mv -f /etc/youtube-auto-publish/material-source.sql.rollback /etc/youtube-auto-publish/material-source.sql
```

This configuration rollout requires no application restart. Do not restore any
database, publishing ledger, or media assets when rolling back this SQL.
