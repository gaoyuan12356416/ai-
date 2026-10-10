# Deployment acceptance: 2026-10-10, Beijing time

Runtime source commit: `098697fe2ee047077f483d90bef61275f0d07fc3`, pushed on
`codex/fb-random-frequency-20261010` and fetched by the CPU host from GitHub
before deployment.

Host: `43.166.187.96`. Backend target: `/opt/fb-auto-post/current`, resolving to
`/mnt/data-disk/root-storage-20260915/rootfs/opt/fb-auto-post/releases/d2a6e91f83ec34f188f41c5d8abb413b0bc1d2b5`.
Only `features/fb_auto_posts/core.py` and `service.py` changed in that composite
backend. The four template list/editor HTML/JS assets were synchronized across
the sidecar, `/root/drama_material_service/static`, and `/usr/share/nginx/html`.

Backup and manifest:
`/mnt/data-disk/fb-auto-post-deploy/frequency-random-20261010/backup-20261010T025620Z`.
This contains the online SQLite backup, source/static backups, file manifest,
prior timer state, and before/after hashes for all template, version, run, task,
due-slot, attempt, ledger and frozen-plan rows. All eight row-set hashes were
identical across deployment. The live database was not restored or rewritten.

The five scheduler/planner/preparer/runner/reconciler timers were briefly paused,
workers drained, and only `fb-auto-post-service.service` restarted successfully
at 10:56:24. Old PID `3669203`, new PID `1032826`; `/health` returned 200 with
both live and prebuild flags enabled. All five timers were restored active.

Validation:

- Existing and new focused Python suites: 124 tests total across the combined
  suite and final added boundary regression, all passed.
- Server Python 3.9: 23 new guard/cutover tests passed, actual compile passed.
- Node whole-page script execution used the real production v6 DTO and fixed,
  random, mixed, zero, fallback, refresh and mode-switch scenarios: passed.
- All 16 deployed files matched the staged commit hashes. All four public
  assets returned HTTP 200 with matching bytes and the new
  `page-frequency-20261010-v1` cache version.
- Live API invalid-cutoff canary returned HTTP 400 `invalid_request` with the
  expected ISO-time validation text; a full template readback was identical.
- Production read-only preflight rehearsed 14 dates in isolated SQLite: every
  date had two stable times, at least 60 minutes apart, within 09:15–21:55.
  Allocation remained 144 Pages at two and one Page at zero, nominal 288/day.

## Pending configuration switch (not yet executed)

As of 10:57:47, production template 1 was still enabled v6 with the original five
fixed candidate times and two-per-Page limits. Today's rows were preserved:
53 published, 205 ready, two unknown, 465 skipped. No extra posts were triggered.

`fb-frequency-random-20261010.timer` is active with next trigger
**2026-10-10 22:30:00 Asia/Shanghai**. Its service has no prior execution.
After today's queue drains, it will save the reviewed random-two config as v7
for scheduling beginning **2026-10-11**. It can refuse a config drift or
unfinished queue without cancelling those tasks. Do not interpret timer
installation as completed configuration activation.

Readback evidence is under
`/mnt/data-disk/fb-auto-post-deploy/frequency-random-20261010/`:
`deployment.json`, `preflight.json`, `template-after.json`,
`acceptance-daytime.json`. The later operation writes `receipt.json`,
`publisher-before.sqlite3` and `acceptance.json`; the latter distinguishes
confirmed frozen next-day due slots from awaiting the natural scheduler.

Local copies are in `D:\codex\artifacts\fb-frequency-20261010`.

## Rollback procedure

Before reverting anything, prevent a new automatic cutover and inspect whether
its service is currently executing:

```sh
systemctl disable --now fb-frequency-random-20261010.timer
systemctl show fb-frequency-random-20261010.service -p ActiveState -p SubState
```

If active, allow its current management transaction to finish and inspect
`receipt.json` plus the live template before proceeding. Do not kill it between
disable/save/enable. For source rollback, pause/drain the same five work timers,
restore the exact files from the backup's `files.json` manifest, restart only
`fb-auto-post-service.service`, verify `/health`, then restore the prior active
timer states. The two source backups are `0-core.py` and `1-service.py` in the
backup directory above. Keep new UI assets if retaining random mode.

If v7 has already been activated, configuration rollback must use the supported
management API to save the old config as a higher version after checking queued
work; do not edit existing versions or restore the SQLite backup. Never run the
dated cutover script on a later date. The backend-maintenance skill project map
was updated with the candidate-window distinction and transaction guard.
