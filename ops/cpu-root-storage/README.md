# CPU root storage relocation, 2026-09-04

Scope: CPU 43.166.187.96 only. No database/schema/publishing changes. Never move
active `.local`, `.codex`, Conda, MySQL or Docker directories in this rollout.

## Cold data

`migrate_cold.py` defaults to read-only inventory/hash validation. `--apply`
relocates the four explicit cold paths; `--attachments --apply` handles only
regular Feishu attachment files older than 7 days, preserving live downloads.
Both modes require the known data-disk UUID and free space. Apply preserves
metadata via rsync, validates hashes and SQLite quick_check, rejects active
handles or changing sources, atomically exchanges the source with a symlink,
then deletes only the verified exchanged root copy. Full data remains under
`/mnt/data-disk/root-storage-20260904/data`, receipts under `audit`.

Never retry after a partial failure without inspecting the receipt and exact
`.migration-swap-20260904` path. `verified_copy` means source unchanged;
`exchanged` means the old copy may remain at the swap path; `complete` means
the verified data-disk target is the live compatible path. No broad cleanup.

Cold rollback: confirm root capacity, quiesce relevant writers, rsync the
receipt destination to a new sibling of source, compare hashes/metadata,
atomically exchange that sibling with the source symlink, retain the data-disk
copy. Do not rollback all items if root lacks sufficient free space.

## Attribution temporary storage

Do not deploy the repository's unrelated application code. Keep the existing
immutable attribution release. Install only this guard and the two scoped
systemd drop-ins after backing up existing units/drop-ins and the prior guard
pointer. Create project tmp directory on the verified mounted disk, root:root
0700. Main service and refresh service get TMPDIR and SQLITE_TMPDIR and fail
closed at startup on wrong UUID, low capacity or unwritable storage. Block root
temporary fallbacks. Preserve the shared refresh flock file in `/tmp` as the
only writable exception, to maintain coordination with existing callers.

Stop only the refresh timer (not a running refresh), wait for in-flight refresh
to finish naturally, install drop-ins, daemon-reload, validate units, restart
only the attribution web service, check `/healthz`, then restore the timer.
Its existing ExecStartPost automatically prewarms the cache. Validate real
queries and `/proc/<pid>/fd` SQLite temp destinations during prewarm/refresh.

Unit rollback: stop refresh timer, drain the running refresh, remove only new
`90-data-disk-temp.conf` drop-ins (or restore exact backed-up versions), restore
prior guard symlink, `systemctl daemon-reload`, restart
`dramawave-attribution-comparison.service`, check `/healthz`, restore prior timer
state. Keep cold migrated data and audit records; these are independent.

Tests: `python3 -m unittest discover -s ops/cpu-root-storage -v`; Linux is
required for the atomic rename-exchange test. No deliberate disk filling,
heavy replay, or business publishing is part of acceptance.
