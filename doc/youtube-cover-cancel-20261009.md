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

Production acceptance evidence is appended after deployment and the five
requested covers have passed real artifact/readback checks.
