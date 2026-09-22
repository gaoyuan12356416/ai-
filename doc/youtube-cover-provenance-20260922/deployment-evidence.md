# Release verification

Runtime commit `fa3394b72930168ae6ea89ab700d21905cec122e` was pushed to GitHub,
fetched on CPU `43.166.187.96`, compared with the exact expected commit, and
deployed from its clean release checkout on 2026-09-22 at 16:38 Beijing time.

The local suite ran 203 tests (3 skips). Production Python 3.9 ran the same 203
tests (1 unavailable historical fixture skipped), including Linux process-group
cleanup and symlink rejection. A real isolated image passed native thread artifact
verification; the reported historical wrong image was rejected by that same check.

Only the three generation modules were installed and the automatic publishing
worker restarted. The API and unified writer kept their PIDs. The pre-existing
inactive legacy publisher remained inactive. The restarted worker is active with
zero automatic restarts. The public page returned 200, anonymous task API 401.

The reported unapproved task was requeued through the normal version-CAS review
action. Its V2 completed naturally at 16:43:05, with a matching native thread
artifact, unchanged frozen reference hash, no retained temporary auth, no approval
and no upload ledger. The original schedule and requirements remained unchanged.
Other already-approved tasks and platform state were not modified by this repair.

Backup:
`/mnt/data-disk/deploy/youtube-auto-publish/backups/cover-provenance-20260922-163842-fa3394b72930`

Rollback while idle:

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/fa3394b72930168ae6ea89ab700d21905cec122e/scripts/deploy_youtube_cover_provenance.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/cover-provenance-20260922-163842-fa3394b72930
```

Rollback restores code only and preserves current databases, assets, schedules,
upload identities and notification state.
