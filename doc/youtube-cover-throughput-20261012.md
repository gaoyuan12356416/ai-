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

Production release identifiers and acceptance results will be recorded after
deployment. Per-task/operator details belong only in the private data-disk
operations evidence.
