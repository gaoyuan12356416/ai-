# YouTube cancelled-cover queue recovery

A task cancelled while its Codex image process was running continued occupying
the single preparation lane until the 20-minute generation timeout. Five newer
tasks created on 2026-10-09 at 10:50-10:53 Beijing time therefore stayed queued.
The cancelled task's status and lease were already correctly cleared; the
missing behavior was propagation of that cancellation to the generator.

Generation now checks the existing SQLite row through a read-only connection
every 0.5 seconds. Cancellation, a changed version, a missing row, or an expired
generation lease stops that attempt without automatic retry. The workflow's
existing lease guard retains the cancelled task instead of recording a new
failure or accepting a late cover. Reference-image freezing, pixel validation,
native-image provenance, cover review, notification deduplication, and all
publication rules remain in force.

Generator cleanup also tracks descendant PID start times. This covers
code-mode children that create a separate POSIX session and would otherwise
survive a kill of only the launcher's process group. Capture-pipe cleanup has
a bounded wait; normal CLI JSON and image-origin evidence remain available.

## Verification

Run `python -m unittest scripts.test_youtube_worker_runtime
scripts.test_youtube_reference_runtime scripts.test_youtube_cover_provenance
scripts.test_youtube_cover_crop scripts.test_youtube_failure_images_runtime`.
Linux checks include live SQLite cancellation, separate-session descendants,
timeout cleanup, preserved stdin/stdout, and successful following work.

## Deployment and rollback

Push the exact commit before checking it out into
`/mnt/data-disk/deploy/youtube-auto-publish/releases/<commit>` on CPU
`43.166.187.96`. Run `python3 scripts/deploy_youtube_cover_cancel.py` there.
Only two Python files in `/root/drama_material_service` are installed after
exact pre-release hashes are checked. A data-disk backup contains both previous
files, a manifest, and an online SQLite backup.

The worker receives SIGTERM through `systemctl kill --kill-who=main` and drains
its current generation/publication before the existing `Restart=always`
restarts it. Verify the new PID and installed hashes before calling it active.
The main API and other publishing services are not restarted.

Rollback from the same release with
`python3 scripts/deploy_youtube_cover_cancel.py --rollback <backup>`.
This checks for subsequent code drift, restores the adapter before its runtime,
and gracefully drains the worker. Retain the current database, generated
assets, review decisions, schedules, notification outbox, and publish ledger;
never restore the database backup over current operational facts.

## Production acceptance, 2026-10-09 11:21 Beijing time

Runtime commit: `f7e7e8f7e5cc173cc30aca085bf387da9c3f1fad`, pushed to
`codex/youtube-cover-cancel-20261009` and fetched on the CPU server before
deployment. Linux ran 119 relevant tests: 118 passed; one historical image
fixture was unavailable and skipped. Local syntax and diff checks passed.

The existing worker finished at 11:15:38; systemd restarted it at 11:15:48.
New PID `66663` is active/running. Both installed file hashes match the GitHub
release. Main API, main job worker, and the unified writer remain active.

All five requested tasks have a current V1 cover. Authenticated task-owner GETs
for `/api/youtube-auto-publish/tasks/<id>` and
`/api/youtube-auto-publish/covers/<asset-id>` returned HTTP 200. All five JPEGs
fully decoded at **1664 x 936**, and returned bytes matched their stored SHA256.
Every generation audit matches its frozen reference SHA and its own isolated
native image artifact; all five source-image SHA256 values are distinct.
No task IDs, schedules, review decisions, or publication facts were reset.

The earliest Korean task's cover was generated and approved, but its later
publication failed while creating a YouTube upload session:
`youtube_resumable_create_failed`, ledger status `failed`, empty `video_id`,
`unknown_outcome=0`. The cover remains available. This is a separate publishing
error; this cover repair does not claim successful video publication.

Private per-task readback evidence is on the CPU data disk at
`/mnt/data-disk/youtube-auto-publish/operations/cancelled-generator-20261009-110631/acceptance.json`.
The pre-operation record in the same directory found no process left to kill:
the cancelled generator exited at its own timeout, and the existing worker
started the first requested cover at 11:06:31. Recovery did not issue a manual
retry, generate replacement task IDs, or change the cancelled row.

Backup:
`/mnt/data-disk/deploy/youtube-auto-publish/backups/cover-cancel-20261009-111416-f7e7e8f7e5cc`.
Exact code rollback on CPU `43.166.187.96`:

```sh
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/f7e7e8f7e5cc173cc30aca085bf387da9c3f1fad/scripts/deploy_youtube_cover_cancel.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/cover-cancel-20261009-111416-f7e7e8f7e5cc
```

Wait for the gracefully drained worker to restart, then verify its PID, service
state and restored file hashes. This restores only the two code files and
retains all current operational data. Skill context was updated in
`ai-backend-maintenance/references/youtube-auto-publish.md`.
