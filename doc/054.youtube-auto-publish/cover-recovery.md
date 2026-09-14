# Failed cover recovery

The 2026-09-12 failure returned exit code zero but left a PNG with invalid caBX CRC and an undecodable chunk stream. The saved success message is not image validity evidence. Original tool/copy traces were not retained, so the precise corruption origin remains unknown.

Generation now instructs binary copying of the tool artifact and full decoding, retries corrupt or missing output once in a separate workspace, and shares a 1200-second total budget (within the existing 1800-second lease). Strict image validation and bounded crop remain unchanged. Invalid normalized output gets private code/size/SHA diagnostics; source bytes are preserved. No process, auth, reference, ratio or platform write failure is blindly retried.

Failed-generation task details expose Upload cover. Selection is local and previewed before explicit confirmation. The existing authenticated upload/review routes validate image bytes and ownership. Only manual review is newly permitted in generation_failed, with current-version CAS and no existing publication ledger. Failed AI versions remain in storage; the replacement creates an approved manual version. Expired reservations enter schedule_missed and require a new scheduling decision. Video/thumbnail/unknown publication failures cannot use this entry point.

Validation: targeted backend suites, browser selection/preview/confirmation with mocked API, and isolated production image-generation validation. No test video, comment or Feishu message is sent.

Deploy scripts/deploy_youtube_cover_recovery.py from an exact GitHub release. It checks live file baselines, data mount, SQL and idle state; backs up code/SQLite/outbox; installs only runtime.py, service.py and page HTML/JS to API/Nginx roots; restarts API and its affected YouTube workers. Unified writer stays running. Rollback: run the same script with --rollback BACKUP; current databases and assets are retained.

## Production verification, 2026-09-14

Runtime release f7e4fe078732e3921111f33d951e781fbab1e70f (code 98b400e494c1953afe42856e271a41c2dd69e586), CPU /root/drama_material_service. Local and CPU each ran 132 targeted backend tests: 131 passed, 1 environment fixture skipped. Chromium desktop/mobile recovery checks: 8 passed, zero JS exceptions. Public HTML/JS HTTP 200 and hashes exactly match deployed bytes; anonymous API 401; original task owner-authenticated GET 200 with can_upload_cover=true. API and two affected workers restarted and active with NRestarts=0, unified writer retained. All publishing/preparation counts and 21 short links unchanged.

Backup: /mnt/data-disk/deploy/youtube-auto-publish/backups/cover-recovery-20260914-101036-f7e4fe078732

Exact rollback:
```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/f7e4fe078732e3921111f33d951e781fbab1e70f/scripts/deploy_youtube_cover_recovery.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/cover-recovery-20260914-101036-f7e4fe078732
```
