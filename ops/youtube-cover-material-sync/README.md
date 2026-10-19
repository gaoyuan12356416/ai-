# YouTube cover material sync

Independent outbox consumer. See [requirements and release evidence](../../doc/20261019.youtube-cover-material-sync/requirements.md).

Run tests: `python -m unittest -v test_sync.py` from this directory.
Commands: `plan --scope approved`, `freeze --scope approved`, `run --max-new 50`, `status`, `export`.
`freeze` is repeatable only against the immutable existing manifest. `run` always discovers published tasks and never expands the historical approved scope.

Runtime: `/mnt/data-disk/youtube-cover-material-sync/{backfill-manifest.json,sync.sqlite3,status.json,export.json}`. The systemd service loads existing backend secrets privately; no credentials are in this repository. Unknown requests are readback-only. The one-item pilot is adopted by its stable external material ID.
