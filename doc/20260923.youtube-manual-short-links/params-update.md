# Manual attribution parameter update - 2026-09-23

New manual operations freeze `version=youtube-manual-link-v2` with `af_c_id=operation_id` (UUID only) and `af_ad_id=yt_manual`. All other query fields remain unchanged. V1 records retain their frozen URLs, including response-loss retries. No automatic publication attribution or frontend assets change.

Local validation: 22 manual-link/deployment tests, 8 automatic-attribution tests and 24 HTTP tests pass (54 total). Tests exercise real isolated SQLite/filesystem generation, concurrent retry idempotency, historical V1 files and failed-operation recovery. The initial Windows test cleanup issue was fixed by explicitly closing SQLite connections. No production test links are generated.

Deploy only `features/youtube_auto_publish/manual_links.py` using `scripts/deploy_youtube_manual_link_params.py --check`, then the same command without `--check`, from a clean exact GitHub release. The expected live SHA is pinned. Existing deployment helpers back up code and SQLite and restart only `drama-material-api.service`.

Rollback uses `scripts/deploy_youtube_manual_link_params.py --rollback BACKUP`; code only, preserving current database and link files. The wrapper stops the API before checking that no incomplete V2 operations remain, because the prior V1 code cannot resume those operations. If the check fails, it leaves current code/data intact and starts the API so those requests can finish before retrying rollback. Published V2 links remain readable after rollback.

Production release, backup and verification will be recorded below after deployment.
