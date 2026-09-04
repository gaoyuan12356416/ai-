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

The historical weekly cache failed pre-migration quick_check (out-of-order
rowids / child page depth mismatch). Its untouched source and copy hashes match.
After auditing that pre-existing condition, the narrowly scoped
`--archive-invalid-weekly-cache --resume-verified-copy` preserves its exact bytes
and original-path symlink, records the failure in its receipt, and treats it
only as an invalid historical archive. It does NOT repair it, certify it as a
healthy database, or let other invalid/live databases bypass validation.

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

Unit rollback: `bash /opt/cpu-root-storage-current/rollback_attribution.sh --apply`.
It checks ownership of both drop-ins, stops the timer, drains refresh, removes
only the storage overrides, reloads systemd, restarts only attribution, checks
health and restores the timer. It intentionally keeps TimeoutStartSec=10min:
the pre-existing warm-up now exceeds the original 90-second startup limit.
Application code, cold migrated data, guard artifact and audit are retained.

Tests: `python3 -m unittest discover -s ops/cpu-root-storage -v`; Linux is
required for the atomic rename-exchange test. No deliberate disk filling,
heavy replay, or business publishing is part of acceptance.
