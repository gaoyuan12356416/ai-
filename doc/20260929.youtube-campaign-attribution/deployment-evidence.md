# Production acceptance — 2026-09-29

Deployed at approximately 20:35 Asia/Shanghai; public readback completed at 20:37. Runtime commit `b37156aaef3fe742c3f66936db64d85982cd0411` was pushed to GitHub, fetched and verified into a clean release on CPU 43.166.187.96. The running directory is `/root/drama_material_service`; only `features/youtube_analytics/report.py` changed.

Backup: `/mnt/data-disk/deploy/youtube-auto-publish/backups/campaign-attribution-20260929-203541-b37156aaef3f`.

## Completed checks

- Windows and production Linux: analytics 57/57, deployment fault tests 12/12. Existing deployment suite 3/3 also passed locally.
- Runtime SHA256 matches the exact GitHub release: `5143768de7d854e6049f5830b87a71362f16d0b4238a91e40fdd7f4fcd40ac17`.
- The API was restarted once, is active, and NRestarts=0. Both publisher workers remained active with unchanged PIDs.
- All 407 pre-deployment short-link records retain identical URL/hash/state projections. No source statistics, channel ownership or publication records were modified.
- Read-only source reconciliation and authenticated options/report/CSV passed for UTC 2026-09-22..28, 09-28 and 09-29. Anonymous access remains 401, invalid dates 400, ordinary users remain owner-scoped, admins tenant-scoped.
- The default seven-day and incident-day process caches were refreshed. Incident links were read back through the public HTTPS API with frozen owner/drama/channel dimensions and new attribution metadata.
- All revenue in these inspected date ranges reconciles. The seven-day range retains one unmatched click and three visits with zero revenue; incident-day source metrics fully reconcile. Unknowns are not assigned by guesswork.
- Private business values and raw evidence are intentionally kept outside Git under `D:/codex/audits/youtube-attribution-20260929/`, including production-acceptance.jsonl and public-readback.json.

This release updates AI-backend analytics only. The separate daily-report timer and previously delivered Feishu report archives were not modified or resent. Upstream mixed metadata remains in the read-only source; the report now resolves by the authorized stable campaign-ID contract.

## Exact rollback

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/b37156aaef3fe742c3f66936db64d85982cd0411/scripts/deploy_youtube_campaign_attribution.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/campaign-attribution-20260929-203541-b37156aaef3f
```

Rollback checks the installed hash before restoring the backed-up file and restarting only the API. It preserves the current database, frozen links and publication state. The ai-backend-maintenance YouTube reference was updated to record this matching contract; memory files were not modified.
