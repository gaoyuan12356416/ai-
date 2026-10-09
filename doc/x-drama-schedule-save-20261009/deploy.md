# Production acceptance, 2026-10-09

The short-drama save repair is deployed on CPU `43.166.187.96`.
Approved GitHub deployment commit: `02ab0404adbdd1c974ee0f5547bb19ac95869a80`.
The previous live main-API app/client composite was preserved before the narrow
repair; unrelated local dirty worktrees and public static files were retained.

Runtime:

- `/opt/x-post-automation/current` resolves to
  `/mnt/data-disk/x-post-automation/releases/02ab0404adbdd1c974ee0f5547bb19ac95869a80-drama-save`.
- Main API files are `/root/drama_material_service/app.py`,
  `features/x_accounts/client.py` and `features/x_posts/service.py`.
- Backup and code-only rollback manifest:
  `/mnt/data-disk/x-post-automation/maintenance/20261009-drama-save-02ab0404/manifest.json`.
- Previous Sidecar release:
  `/mnt/data-disk/x-post-automation/releases/8f9b2fc-suspended-account`.

Local verification passed 328 tests. Linux acceptance passed 298 relevant store,
OAuth/account, scheduler and API mapping tests. The pre-existing source/public
material-pool UI difference described in README was retained. Syntax and diff
checks passed. Sidecar/API restarted and reached readiness; public OAuth health
and the short-drama page returned 200, and anonymous schedule PUT returned 401.
Main/Sidecar file hashes match the approved Git commit. Live SQLite quick_check
is `ok`, foreign-key violations are zero, and token hashes/modes are unchanged.

Read-only production-ledger replay excludes unavailable owners 8 and 10, saves
the remaining 17 accounts with one random daily batch in memory, and computes
the next-day effective date as 2026-10-10. All drama/account/queue/Post/Repost/
delivery-route fingerprints remain unchanged. The current candidate count is
zero; the repair does not manufacture materials or replay historical failures.

The production schedule itself was not submitted by the deployment audit. Its
saved version remains 24, with 19 accounts and two random daily batches. The
operator's 17-account/one-batch draft can now be resubmitted using the regular
authenticated page. X Auto scheduler/runner timers returned to active; previously
inactive material/drama/manual publisher timers remain inactive.

Exact rollback command:

```bash
python3 /mnt/data-disk/x-post-automation/releases/02ab0404adbdd1c974ee0f5547bb19ac95869a80-drama-save/scripts/rollback_x_drama_schedule_save.py --manifest /mnt/data-disk/x-post-automation/maintenance/20261009-drama-save-02ab0404/manifest.json
```

This drains active Auto work, restores only the backed-up main code and previous
Sidecar symlink, restarts Sidecar/API, checks readiness and restores prior active
Auto timers. It never restores an old database or token directory.

Local operator evidence:
`D:\codex\artifacts\x-publish-audit-20261009\deployment.json` and
`post-deploy-save-replay.json`. X publishing/AI backend skill context was updated
in place while retaining the pre-existing private skill-store working changes.
