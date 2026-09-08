# TT Minis database and analysis storage

For TT Minis analysis in this workspace, use the existing main cache through
`/root/codex_test/data/tt_minis_multi_dim_dashboard_tti_app_revenue_cache.sqlite3`.
It resolves to `/mnt/data-disk/projects/codex_test/data/`.

- Never copy or back up TT Minis databases into `/tmp`, `/var/tmp`, `/root`, or a root-backed directory, including timestamped one-off files.
- Before TT Minis analysis, run `python3 /root/codex_test/tt_minis_storage.py check`. A missing/wrong/unwritable/full data disk means stop, never fall back to root.
- For a consistent local analysis snapshot, run `python3 /root/codex_test/tt_minis_storage.py snapshot` and use the returned path. Do not use `cp` on the live SQLite database.
- Run analysis commands through `python3 /root/codex_test/tt_minis_storage.py exec -- <command> ...` so SQLite sorting and Python temporary files also use the data disk.
- Store reports, backups, and task outputs under `/mnt/data-disk/tt-minis-storage/` or another verified data-disk task directory.
- Preserve compatibility symlinks in `/tmp/tt_minis_cache_0907*.sqlite3`; do not unlink and recreate those paths. The direct-copy file from 2026-09-08 was already malformed and is archival evidence only.
- Never delete active snapshots automatically. Check references and open files before retiring old snapshots.
