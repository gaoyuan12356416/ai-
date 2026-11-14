Social daily report repair 2026-11-14

The scheduled collector's unscoped Facebook legacy source query timed out twice
at its existing 20-second statement budget. No report batch or send was frozen.
Freeze globally eligible backend source triples before querying; read only their
Page candidates and retain exact campaign/Page/material tuple filtering, date
coverage sentinels, owner verification, row limits and financial reconciliation.
No SQL timeout, retry, source, date, attribution or delivery-idempotence policy changes.

Deployment: overlay committed backend_scope.py and collector.py on the existing
independent report runtime, after backup. Leave all publisher services unchanged.
Rollback: restore prior current symlink; never restore old sent delivery ledgers.
Validation: 15 source-boundary tests and 58 daily report tests passed.
