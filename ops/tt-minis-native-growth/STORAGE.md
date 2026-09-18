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

## Snapshot lifecycle (2026-09-18)

`snapshot --owner <analysis-task>` still prints exactly one SQLite path. Requests
serialize on a kernel lock and reuse a validated snapshot when the source DB,
WAL/journal and snapshot identity have not changed. A source changing during
backup produces a valid snapshot but is deliberately ineligible for reuse.

New snapshots have a 24-hour lease, seven-day idle retention, and a 40 GiB
managed-pool ceiling. The latest two, open files, explicit pins, code/config
references and active leases are protected. If the ceiling cannot be met safely,
creation stops; it never evicts protected inputs or falls back to the root disk.
Legacy/unregistered snapshots are excluded from automatic deletion and the
managed-pool ceiling; retire those only from an explicitly audited inventory.

For a longer analysis: `lease <snapshot> --owner <task> --hours 72`.
For permanent evidence: `pin <snapshot> --owner <reason>`; remove that explicit
pin with `unpin <snapshot> --owner <reason>` when no longer needed. References
outside the configured code/config roots must use a lease/pin. Do not create
new full copies manually or modify immutable registered snapshots.

`prune` previews managed cleanup; `prune --apply` performs it. Hourly low-priority
maintenance uses this entrypoint. All lifecycle operations share the same lock.
`retire --manifest <audited.json>` previews legacy cleanup; adding `--apply`
rechecks source references, latest two, active descriptors, leases/pins, exact
inode/size/mtime/link count and path containment immediately before retirement.
The audit inventory and execution receipt must be retained outside the snapshots.

## Incremental publication

The 60-day/two-level data contract and 24-hour reader grace stay unchanged.
Each partition records a format/schema signature plus refresh-log timestamp and
row count. An unchanged partition reuses its immutable path and is not read from
SQLite again; historical backfills invalidate just the affected partitions.
Missing/truncated files rebuild. Missing/changing revisions or mismatched row
counts abort before replacing `latest.json`.

The first upgraded publication builds the version index once. Old manifests are
archived at retirement and protect all referenced files for another 24 hours,
regardless of detail file age. Malformed history disables cleanup. Never run
mtime-only cleanup independently against the shared immutable partition pool.
Changing serialization/calculation semantics requires bumping
`PARTITION_FORMAT_VERSION` even when columns do not change.

`partition_publish` logs written/reused partitions, bytes, cleanup count and
elapsed seconds. Snapshot retention does not change business-cache retention.
