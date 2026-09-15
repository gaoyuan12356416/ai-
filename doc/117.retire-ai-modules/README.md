# Retire four AI backend modules

## Production acceptance — 2026-09-15

- CPU: `43.166.187.96:/root/drama_material_service`.
- Deployed runtime commit: `d697e30e6773502c13a5187c0cc3a0fde2a9b1a8`.
- Branch: `codex/retire-ai-modules-20260915`; GitHub push and server checkout verified.
- Backup: `/mnt/data-disk/retired-ai-modules-20260915/backup-20260915T084944Z`.
- All 72 planned file hashes matched. Four unit/timer entries are inactive and masked;
  the legacy cron entry is absent; ports 8792/8793 are closed.
- Nginx validation/reload and the worker-aware main API restart succeeded. Eight
  preserved production services are active. Public Meta cleanup, synthesis, screenshot,
  voiceover, TikTok pool, YouTube, and auth paths returned 200.
- Nineteen final route checks passed, including GET/POST retirement fences on both
  public and loopback paths and retained playable/Meta cleanup routing.
- Production material history remains 37 tasks with unchanged status counts.
- Removed test-process cgroup memory: 47,341,568 bytes (45.1 MiB).
- Original test tree: 115,486,720 allocated bytes. Its compressed archive is 46,989,823
  bytes, with 603 regular-file checksums verified before the source tree was removed.
  Five directories containing only Python bytecode were also removed. Net reduction
  for those directories, after deducting the archive allocation: 82,227,200 bytes
  (78.4 MiB). This is not a claim about whole-host free space, which changes with
  ongoing jobs and deployment backups.
- Archive SHA-256: `6a9ac77264a3880810b918312c2e8a6062a82a297748eea554a0a950260ecbe5`.
- Private machine-readable evidence: `plan.json`, `verification.json`, and
  `reclaim.json` under the backup directory.
- Browser inventory failed twice because the browser connector could not load its
  request-header policy. Visual screenshot acceptance was unavailable; live public
  navigation JSON, production HTTP responses, and navigation renderer tests passed.

### Exact rollback after reclamation

Only use this procedure when intentionally restoring the four retired modules.
It restores the archived test environment, then invokes the checked code/configuration
rollback. Production SQLite and MySQL history are not replaced.

```bash
set -e
backup=/mnt/data-disk/retired-ai-modules-20260915/backup-20260915T084944Z
release=/mnt/data-disk/retired-ai-modules-20260915/releases/d697e30e6773502c13a5187c0cc3a0fde2a9b1a8
test_parent=/mnt/data-disk/root-storage-20260915/rootfs/root
test_path=$test_parent/drama_material_service_test
test ! -e "$test_path"
test ! -e /root/drama_material_service_test
test ! -L /root/drama_material_service_test
printf '%s  %s\n' 6a9ac77264a3880810b918312c2e8a6062a82a297748eea554a0a950260ecbe5 "$backup/material-test-environment.tar.gz" | sha256sum -c -
tar -xzf "$backup/material-test-environment.tar.gz" -C "$test_parent"
ln -s "$test_path" /root/drama_material_service_test
mv "$backup/reclaim.json" "$backup/reclaim.restored.json"
python3 "$release/scripts/deploy_retired_modules.py" rollback "$backup"
```

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
