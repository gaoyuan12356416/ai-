# Deployment evidence

- CPU deployment completed at **2026-10-15 15:56:19 Asia/Shanghai**.
- Branch: `codex/youtube-channel-cover-20261015`.
- Runtime commit: `523903e2ddd75e2a508ada355b2d8f6596de3da3`, pushed to GitHub and fetched/verified into the CPU release checkout.
- Runtime: `43.166.187.96:/root/drama_material_service`; public static files: `/usr/share/nginx/html`.
- Release: `/mnt/data-disk/deploy/youtube-auto-publish/releases/523903e2ddd75e2a508ada355b2d8f6596de3da3`.
- Backup: `/mnt/data-disk/deploy/youtube-auto-publish/backups/channel-cover-20261015-155618-523903e2ddd7`.

## Verification

- Local: 106 backend tests passed (12 template, 24 HTTP, 45 preparation, 19 channel-directory, 3 existing deployment and 3 scoped deployment). Required Python compilation, changed JavaScript syntax checks and diff whitespace checks passed.
- CPU Python 3.9: 12 template tests and 3 scoped-deployment tests passed before install. Installer `--check` passed for 11 files after two migrations of an isolated online database copy.
- Both local and deployed public pages passed the same 41 browser assertions, including save/reopen/clear/conflict, channel switching, empty defaults, per-publication editing, stale/error replies, old GET response compatibility, local-cover behavior, final payload and desktop/mobile layout. All browser API responses were mocked; production template/task POSTs were never sent.
- Installer verified the data-disk UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`, file hashes, online DB backup and rollback manifest. Migration added column `cover_requirements_template TEXT NOT NULL DEFAULT ''`; all 31 existing template rows retained their original fields, versions and metadata. Their original-field SHA-256 remained `87f63a64c9bdf6cb93e5d2b4ba83cb4ecf7d95653ba0296860184db328ecf421`; the new field is initially empty for all 31.
- Only `drama-material-api.service` restarted: PID `1171997` -> `2640208`, active, `NRestarts=0`, health endpoint HTTP 200. Startup logs showed the API listening normally.
- `youtube-auto-publish-worker.service` PID `3432663` and `drama-youtube-unified-writer.service` PID `2937249` remained active and unchanged. The previous publisher remained inactive with PID 0. Protected `app.py`, workflow service, generation runtime, worker runtime and worker script SHA-256 values remained unchanged.
- All five public versioned static assets returned HTTP 200 with bytes matching the exact release. Anonymous template GET returned 401. Read-only verification through an existing authenticated admin session returned 97 channels and HTTP 200 for a template, including the new string field. Credentials stayed in process memory and were not logged.
- No test production template, publishing task, AI cover, notification, video upload or comment was created. Existing publisher state was preserved.
- The AI-backend skill's `references/youtube-auto-publish.md` was updated with the reusable default/migration/compatibility/rollback rules. Prior local skill edits were preserved and the pre-change reference was backed up locally.

## Exact rollback

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/523903e2ddd75e2a508ada355b2d8f6596de3da3/scripts/deploy_youtube_channel_cover.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/channel-cover-20261015-155618-523903e2ddd7
```

Rollback refuses later file drift. Retain the current database and additive column, including newly saved cover defaults, assets, tokens, outbox and publishing facts. Restore code/static files only; never replace live SQLite with the backup.
