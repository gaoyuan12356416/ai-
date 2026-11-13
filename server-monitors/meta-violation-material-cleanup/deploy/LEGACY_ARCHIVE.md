# Frozen legacy ledger archive

Only the disabled legacy ledger at the root `runs.sqlite3` is eligible. The
active `cover.sqlite3`, `cover-audit/runs.sqlite3` and shared deletion ledger
remain in place. Confirm legacy service/timer and historical repair/deployment
helpers are inactive, no process holds the ledger, and no WAL/journal exists.

Before archiving, write a durable mode-0600 `runs.sqlite3.archive.json` beside
the ledger with its exact SHA256, size, private archive receipt/manifest path
and these restoration instructions. Current Runtime refuses every open while
this marker exists, including an accidentally recreated database. Archive and
remove the original only through the verified private storage archive workflow.

Restore while all legacy services and helpers remain disabled. Fetch and verify
the exact archived object into a separate absent staging filename. Require the
original database path to be absent, then publish the verified file there
without overwriting any concurrent file. Recheck its
SHA256, size and ownership/mode; only then remove the archive marker. Enabling
the retired workflow requires its own authorization. Code rollback alone must
keep the marker until this data restore is complete. Historical releases from
before this guard must never be invoked against an absent legacy ledger:
restore and verify the original ledger first. Current cover rollback does not
enable the retired legacy timer or restore historical ledger copies.
