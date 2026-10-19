# Production acceptance — 2026-10-19

Deployed to `43.166.187.96:/root/drama_material_service` and `/usr/share/nginx/html` from GitHub: feature `dd91f7b0eeef61c67de52ed8d104fd305b5a7f65`, page-lookup optimization `c5dc859ec0b76037e5cd19c4e993c0634d5da92f`. Both exact commits were fetched and checked out on CPU; hash-guarded preflights passed.

Validation: 109 targeted tests passed on Windows and production Python; after the page-bound regression was added, all 63 analytics/metadata tests passed on both systems. All three page scripts passed Node syntax checks. Public authenticated report rendered the added English-title column. Examples: Spanish Ríndeme a tus pies, mi reina maps to Rein Me In, My Queen; the screenshot's traditional-Chinese drama maps to I Got Three Girlfriends From the System. Channel template HTTP GET returned 200 and the additive language field; language save/clear, tenant sharing/isolation, stale-version rejection and older-client omission are covered by isolated SQLite tests. No production language was assigned on the user's behalf.

Authenticated live 2026-10-12 through 2026-10-18 UTC report/CSV: 50 current-page rows, 1068 exported groups, 1050 with resolved English titles, 18 explicit missing/ambiguous labels, zero unavailable lookups. CSV click/view/install/conversion sums exactly match report totals. Warm requests measured 0.176s and 0.170s (loopback, not end-user latency).

Only main API restarted; active PID 3627143. Auto-publisher PID 3432663 and unified writer PID 2937249 remained unchanged. No test publishing, messaging or historical rewrites. Skill reference updated: ai-backend-maintenance/references/youtube-auto-publish.md.

## Backups and rollback

Feature backup: `/mnt/data-disk/deploy/youtube-auto-publish/backups/channel-templates-20261019-122217-dd91f7b0eeef`. Optimization backup: `/mnt/data-disk/deploy/youtube-auto-publish/backups/channel-templates-20261019-122320-c5dc859ec0b7`. Both contain exact replaced source and an online SQLite backup.

To roll back both in reverse order, preserving current data:

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/c5dc859ec0b76037e5cd19c4e993c0634d5da92f/scripts/deploy_youtube_title_lookup_bounds.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/channel-templates-20261019-122320-c5dc859ec0b7
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/dd91f7b0eeef61c67de52ed8d104fd305b5a7f65/scripts/deploy_youtube_display_metadata.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/channel-templates-20261019-122217-dd91f7b0eeef
```

Rollback refuses later source drift. Do not restore the SQLite backup over live publishing state.
