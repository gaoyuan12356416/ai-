# YouTube cover queue dispatch repair

## Problem and change

The worker previously shared one preparation slot across all operators. After
that slot completed, only the publication main loop could dispatch another
preparation. A long video upload, processing readback or exception before the
dispatch tick could therefore leave queued covers idle even with no generator
running. An operator's filtered list could also show only waiting tasks while
another operator occupied the single global slot.

Preparation now continuously claims work in two independent bounded slots.
`YOUTUBE_AUTO_PREPARATION_WORKERS` accepts integers 1–4, default 2. The slots
refill without waiting for a publication iteration. Empty queues and dispatch
errors back off for one second. Both existing enable flags gate new claims.
SIGTERM disables new claims and drains existing work before process exit.

Claims still use the existing SQLite transaction and lease. No database schema,
task data, generator prompt, image source, reference validation, cancellation,
review, scheduling, notification outbox or publication retry policy changes.
Each generator retains its task/version/attempt-specific isolated workspace.
The publication engine remains serialized. Failed/unknown publications are not
reset, and rejected generation tasks are not automatically retried.

## Verification

- Local worker/workflow suite: 57 tests, 54 passed; 3 Linux process tests skipped.
- Tests cover slot limits, refill without main-loop ticks, dispatch failure
  isolation, disabled flags, graceful drain and the existing process cleanup.
- A real temporary SQLite workflow verifies two simultaneous generating rows,
  exactly one claim per task, distinct stored cover assets, notification
  deduplication and mandatory review with zero publication enqueues.
- Run the same suite and cover image/reference/cancellation regression tests on
  Linux before deployment. Production acceptance must check active processes,
  persisted task transitions, actual image decoding and owner-authenticated
  cover HTTP responses; an active service alone is insufficient.

## Deployment and rollback

Only `features/youtube_auto_publish/worker_runtime.py` and
`scripts/youtube_auto_publish_worker.py` are deployed from the exact GitHub
commit to CPU `43.166.187.96:/root/drama_material_service`.

`scripts/deploy_youtube_cover_throughput.py` checks expected production hashes,
the data disk UUID and free capacity, backs up the two files and live SQLite,
then installs and compiles the files. It sends SIGTERM to the worker MainPID
and lets `Restart=always` restart after existing work finishes. The main API,
legacy publisher, unified writer and other services are not restarted.

Rollback uses the same release script with `--rollback <backup-directory>`.
It verifies installed hashes to prevent overwriting later releases, restores
only the two previous source files, then drains the worker. Keep the current
database, generated covers, approvals, schedules and publication ledger.
Never restore the database backup over live publication state.

## Production release, 2026-10-12 Beijing time

Runtime commit: `b8c8e3c93b899ca9e743d49d44dd36b17f05ccd7`, pushed to
`codex/youtube-cover-throughput-20261012` and fetched/checked out on CPU before
deployment. Linux regression: **115 tests, 114 passed, one unavailable historical
image fixture skipped**. Both installed source hashes match the release.

Code was installed at 18:45:09; the old worker drained its current task, and the
replacement started at 18:46:58 with PID `3432663` and `slots=2`. Two separate
live Codex process trees and two persisted `generating` tasks were verified.
The main API retained PID `3258648`; the unified writer retained PID `2937249`.
Both remain active. The public page returns 200 and anonymous task access 401.

The live service includes newer channel-template methods absent from this
branch's base. A source comparison confirmed that its preparation/claim path is
unchanged; the service module was intentionally not deployed.

Backup:
`/mnt/data-disk/deploy/youtube-auto-publish/backups/cover-throughput-20261012-184509-b8c8e3c93b89`.

Exact rollback command on CPU:

```sh
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/b8c8e3c93b899ca9e743d49d44dd36b17f05ccd7/scripts/deploy_youtube_cover_throughput.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/cover-throughput-20261012-184509-b8c8e3c93b89
```

Wait for the worker's replacement PID and verify hashes/health. This restores
only dispatch source code, preserving current database and images. Private
per-task evidence is under
`/mnt/data-disk/youtube-auto-publish/operations/cover-throughput-20261012/`.
The maintenance skill's YouTube context now documents independent dispatch,
bounded concurrency and graceful migration.

## Final acceptance, 2026-10-12 18:57:51 Beijing time

All five tasks in the operator screenshot now have a current V1 AI cover.
Public HTTPS task-owner GETs for all five task details and all five cover assets
returned **200**. Every JPEG fully decoded at **1664 × 936**, and its response
bytes and stored file matched the database asset SHA256.

For each cover, verification matched the frozen reference SHA, the original
native image in that attempt's isolated Codex home and its recorded generation
thread. Re-running the same normalization/JPEG conversion reproduced each
stored asset SHA exactly. The five native image hashes are distinct.

The global preparation query returned no queued, generating, review or
enqueue-pending tasks at that cutoff. All five had advanced through the existing
review/publishing flow; the task DTOs showed four published and one still
uploading. This verifies cover recovery, without claiming that the final video
upload had completed. The repair performed no manual retry, task reset, cover
approval or direct platform write.

Final private evidence:
`/mnt/data-disk/youtube-auto-publish/operations/cover-throughput-20261012/acceptance-185755.json`.
