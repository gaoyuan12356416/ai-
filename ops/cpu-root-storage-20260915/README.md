# CPU root storage migration, 2026-09-15

Scope: migrate the explicitly approved non-system directories on CPU 43.166.187.96 to the mounted data disk. Keep the OS, shared system log infrastructure and `/swapfile` on root. Preserve public URLs and old filesystem paths through atomic compatibility links.

The user explicitly approved the staged migration, including brief service stops for shared environments, Codex state, local MariaDB and Docker. This approval does not authorize new public posts or replaying uncertain publication jobs. Drain existing work before stopping its workers.

## Operation

Use the exact GitHub commit containing this directory. Keep code, audit receipts, state snapshots and rollback data under `/mnt/data-disk/root-storage-20260915`. Every write validates the expected mount UUID and at least 5 GiB available space.

1. Record active service/timer/container states and health baselines. Pre-copy with `migrate.py presync SOURCE` while services run. The copy is not yet a usable database backup.
2. Back up affected systemd units/drop-ins and install `90-data-disk-required.conf` for dependencies. Preserve unrelated configuration.
3. Stop the relevant timer or scheduler, let active jobs finish, and stop only the processes referencing that source. The tool rejects cutover if any fd, mmap, executable or cwd still references it.
4. Run `migrate.py cutover SOURCE`. This checks SQLite, synchronizes again, compares full SHA256/owner/mode/time manifests, rechecks source stability, then atomically exchanges the source with a symlink. The old root copy remains under `SOURCE.root-storage-20260915-original`.
5. Restore previously active services and timers; check readiness, real filesystem locations, database health and natural writes. Do not generate external business writes merely to test migration.
6. After acceptance, run `migrate.py finalize SOURCE`. It verifies the original is unused/unchanged, copies it to a private rollback snapshot on the data disk, hashes the copy, and deletes only the exact exchanged root copy. Audit receipts move to `complete`.

Do not use `finalize` as a substitute for business acceptance. Database and Codex process restart handling remain explicit operator responsibilities. Runtime sockets are transient; drain and stop their owners before the final copy.

## Rollback

Stop the dependent writers and run `python3 /mnt/data-disk/root-storage-20260915/repository/ops/cpu-root-storage-20260915/migrate.py rollback SOURCE`. It copies the current data-disk state back to root (requiring capacity), verifies it and atomically restores the original path. Then restore the backed-up unit configuration and previous service states. Do not overwrite current data with an older snapshot without separate reconciliation.

The `backups/` and `audit/` directories contain operational state and may include secrets or databases. Never commit or publish them.
