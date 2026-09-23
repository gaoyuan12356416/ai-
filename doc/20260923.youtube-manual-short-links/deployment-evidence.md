# Production evidence - 2026-09-23

The subsequent manual attribution parameter update is documented in [params-update.md](params-update.md); the evidence below describes the initial feature deployment.

- Runtime commit: `393e3e280e2ce030c556f8204b512f52bf146ad4`.
- Branch: `codex/youtube-manual-short-links-20260923`; draft PR: https://github.com/gaoyuan12356416/ai-/pull/5 .
- CPU host: `43.166.187.96`; runtime `/root/drama_material_service`; public static `/usr/share/nginx/html`.
- Release fetched from GitHub and checked out at exact SHA: `/mnt/data-disk/deploy/youtube-auto-publish/releases/393e3e280e2ce030c556f8204b512f52bf146ad4`.
- Backup at 12:41:36 Beijing: `/mnt/data-disk/deploy/youtube-auto-publish/backups/manual-links-20260923-124136-393e3e280e2c` (files manifest, online SQLite backup and verification.json).

## Verification

- Server Python 3.9: 531 tests run, 530 passed, one historical fixture skip. Local Windows: 529 passed, two platform/fixture skips. Browser: 90 mock-API checks across manual links, channel templates, schedule and loading races; desktop/mobile screenshots inspected.
- Nine installed targets match the release/approved transformation. Public HTML/JS/CSS return 200 and match installed SHA. Anonymous new GET endpoints return 401. The existing service app is patched only at the exact authenticated route anchor; unrelated live changes remain.
- Existing ordinary-user Cookie used only in process memory for read-only HTTP checks; no session or credentials printed/saved. Exact-ID and Chinese-title search each returned the expected drama, HTTP 200/no-store. Channel readback completed: 83 total, 45 eligible, checking=false, no error. Safe channel DTOs contain no scopes, internal account IDs or credentials. These counts are a deployment snapshot, not permanent channel eligibility.
- SQLite quick_check=ok. Publication ledger 213 -> 213; short links 211 -> 211; preparations 206 -> 206. All historical short-link identities/URLs/wrapper hashes and preparation request/copy fields are unchanged. Manual-link table exists with zero rows: no production test link, video, comment or notification was created.
- API PID 1587303 -> 2399399, active/NRestarts=0. Automatic publish worker PID 1482318 unchanged; unified writer PID 2937249 unchanged, both active. Old publisher remains inactive/PID=0. No Nginx restart/reload.

## Exact rollback

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/393e3e280e2ce030c556f8204b512f52bf146ad4/scripts/deploy_youtube_manual_links.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/manual-links-20260923-124136-393e3e280e2c
```

Rollback refuses later file drift. It restores code/static and starts API in finally, preserving the current SQLite database and every generated short-link file. Never restore before.sqlite3 over subsequent production facts.

Skill context updated in ai-backend-maintenance/references/youtube-auto-publish.md; memory storage was not edited.
