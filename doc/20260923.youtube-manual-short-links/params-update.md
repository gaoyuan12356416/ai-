# Manual attribution parameter update - 2026-09-23

New manual operations freeze `version=youtube-manual-link-v2` with `af_c_id=operation_id` (UUID only) and `af_ad_id=yt_manual`. All other query fields remain unchanged. V1 records retain their frozen URLs, including response-loss retries. No automatic publication attribution or frontend assets change.

Local validation: 22 manual-link/deployment tests, 8 automatic-attribution tests and 24 HTTP tests pass (54 total). Tests exercise real isolated SQLite/filesystem generation, concurrent retry idempotency, historical V1 files and failed-operation recovery. The initial Windows test cleanup issue was fixed by explicitly closing SQLite connections. No production test links are generated.

Deploy only `features/youtube_auto_publish/manual_links.py` using `scripts/deploy_youtube_manual_link_params.py --check`, then the same command without `--check`, from a clean exact GitHub release. The expected live SHA is pinned. Existing deployment helpers back up code and SQLite and restart only `drama-material-api.service`.

Rollback uses `scripts/deploy_youtube_manual_link_params.py --rollback BACKUP`; code only, preserving current database and link files. The wrapper stops the API before checking that no incomplete V2 operations remain, because the prior V1 code cannot resume those operations. If the check fails, it leaves current code/data intact and starts the API so those requests can finish before retrying rollback. Published V2 links remain readable after rollback.

## Production evidence

- Deployed at 14:47 Beijing, 2026-09-23, to `43.166.187.96:/root/drama_material_service`.
- GitHub release: `522f2d382a0b61c77ce7bc28d0582d9c7bfe389e`, branch `codex/youtube-manual-short-links-20260923`, PR https://github.com/gaoyuan12356416/ai-/pull/5 . Server fetched and checked out this exact commit. The same 54 tests pass on server Python 3.9.
- One installed file matches release SHA-256 `b4bff405abb68508ed7e811b72b5b055e876d8f3424678c95c0a0253e2de5d8c`.
- Backup: `/mnt/data-disk/deploy/youtube-auto-publish/backups/manual-links-20260923-144701-522f2d382a0b`; contains code manifest, online SQLite backup and `params-verification.json`.
- All 215 pre-deploy short-link identities/URLs/wrapper hashes and the one manual record remain unchanged. Existing manual public HTML matches its frozen SHA-256. SQLite quick_check is ok. Live URL builder produces the two new fields; all other decoded query fields match V1. No production test links created.
- API restarted, active, PID `2399399 -> 2492911`; health HTTP 200, anonymous channel/catalog endpoints HTTP 401. Publish worker PID `1482318` and unified writer PID `2937249` unchanged; old publisher stays inactive/PID 0. No frontend or Nginx change.

Exact rollback (retains current DB and all generated links):

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/522f2d382a0b61c77ce7bc28d0582d9c7bfe389e/scripts/deploy_youtube_manual_link_params.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/manual-links-20260923-144701-522f2d382a0b
```
