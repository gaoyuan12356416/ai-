# Retire four AI backend modules

## Scope

Retire AI自动规则调控, AI自动调控 V3, 投放素材, and 投放素材测试 on the CPU backend.
The operator confirmed that the former US GPU has been reclaimed or repurposed;
it is outside this rollout.

- Remove the four groups from live navigation, saved configurations, and browser fallbacks.
- Keep Meta 广告清理 in its independent `fb_ad_asset_delete` group, including migration
  from historical navigation where it was nested below ad control.
- Return HTTP 410 from the public edge and the main API before retired routes dispatch.
- Keep the shared playable-preview API, published material files, screenshot/cover,
  drama synthesis, voiceover, and social publishing routes available.
- Disable and mask the V3 timer/runner and both dedicated material-test services.
- Remove only the exact legacy ad-control cron entry. Both scheduler entry points
  become no-op retirement notices, so stale manual invocations make no Meta writes.
- Disable startup recovery and asynchronous submission for ad-material generation.
- Preserve SQLite/MySQL history, review records, source references, and finished assets.

## Deployment

The production monolith matches the reviewed `5056aaaf...` app and `cd497200...`
navigation baseline. Work from a clean, exact GitHub checkout beneath
`/mnt/data-disk/retired-ai-modules-20260915/releases`.

```bash
python3 scripts/deploy_retired_modules.py prepare /mnt/data-disk/retired-ai-modules-20260915/releases/COMMIT
python3 scripts/deploy_retired_modules.py apply /mnt/data-disk/retired-ai-modules-20260915/backup-TIMESTAMP
```

Prepare checks source hashes, creates online SQLite backups, saves unit/cron states,
and stages only the changed files. All backups remain mode 0700 on the verified data
disk. Apply removes future scheduling, waits up to 30 seconds for an existing runner,
stops the dedicated test services, masks the four units, deploys the reviewed files,
checks Nginx, reloads it, and uses the worker-aware main API restart helper.
Current live HTML receives only a new shared-navigation script version; unrelated
page content is preserved.

Before API restart, check that the independent Meta cleanup worker has no active
request, and that production synthesis uses the external worker. Do not restart the
drama observer, image generators, or social publishing services.

## Resource reclamation and rollback

After acceptance, delete only proven disposable V3 Python caches and archive the
stopped material-test directory before removing its active copy. Keep the archive
and exact original/real path in the private backup manifest. No production asset
directory or business ledger is a disposable cache.

Before resource reclamation, rollback is:

```bash
python3 scripts/deploy_retired_modules.py rollback /mnt/data-disk/retired-ai-modules-20260915/backup-TIMESTAMP
```

After the test directory has been archived, first restore the archive to its recorded
real path and recreate its original symlink, then rename `reclaim.json` to
`reclaim.restored.json` and run the same rollback command. The rollback checks current
file hashes, restores code/configuration, and merges only the retired cron entry back
into the current crontab. It does not overwrite current SQLite or remote MySQL data.

## Validation

`python scripts/test_retired_modules.py` verifies route isolation, historical
navigation migration, startup recovery suppression, and inert scheduler entry points.
`node scripts/test_retired_navigation.js` verifies removal from fallback/cached menus,
retention of other groups, and idempotence. Run syntax checks, `nginx -t`, public and
loopback 410 checks, permitted-page checks, and confirm inactive+masked units, absent
8792/8793 listeners, and absence of the legacy cron entry.
