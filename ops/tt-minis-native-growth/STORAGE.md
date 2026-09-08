# CPU-host storage protection

The live TT Minis cache already resides at
`/mnt/data-disk/projects/codex_test/data/tt_minis_multi_dim_dashboard_tti_app_revenue_cache.sqlite3`.
The root incident on 2026-09-08 came from two ad-hoc analysis copies in `/tmp`,
not the scheduled dashboard's main database.

Install `tt_minis_storage.py` beside the deployed report scripts. Prefix active
TT Minis cron Python invocations with
`python3 /root/codex_test/tt_minis_storage.py exec --`.
Keep existing schedules, command flags and flock paths unchanged.
This verifies the mounted UUID, available capacity and database device before
executing the original command, and sets TMPDIR/SQLITE_TMPDIR on the data disk.

Install the rules from `TT_MINIS_STORAGE_AGENTS.md` in the server workspace
`/root/codex_test/AGENTS.md`, preserving any existing instructions.
Use the `snapshot` action for future consistent analysis copies, never `cp`
against a live SQLite database. `check` is read-only except creation of the
small configured temporary directory if absent. Failed snapshots remain as
`.partial` evidence and are not promoted to usable `.sqlite3` names.

Migration audit: `/mnt/data-disk/tt-minis-storage/migration-20260908/`.
Both historical copies were SHA-256 verified before symlink replacement and
removal of the exact root duplicate. The raw `cp` copy was already malformed;
the SQLite `.backup` copy passed quick_check. No main DB was replaced.

Rollback: restore the saved crontab and workspace AGENTS.md from the migration
audit. Leave the migrated data and compatibility links in place. If reverting
storage is ever necessary, stop users of the exact snapshot, check sufficient
root capacity, copy it back with checksum verification, then replace only its
compatibility link. A reverse data copy should not be the default rollback.

Validation must include mount refusal, low-space refusal, root-path refusal,
an actual snapshot backup/check on the server, a SQLite write using the exec
entrypoint, and post-migration filesystem usage. Do not send test business
messages or rebuild/publish a report merely to test this storage change.
