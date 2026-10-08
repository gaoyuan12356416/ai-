# Operator-selected FB material round

The root-only CLI `scripts/fb_auto_post_manual_material_batch.py` previews and
reserves one manual run. Each eligible Page receives one material in its own
language. Pages are sorted numerically; materials rotate in input order. The
send order interleaves en, es, zh-tw, id and th, omitting exhausted languages.

Use `--preview --template-id ID --material-ids ID ... --operation-id UNIQUE
--waive-series-blacklist SERIES --waive-drama-cooldown --output MANIFEST` only
when the operator has explicitly authorized those batch-specific exceptions.
Then `--apply MANIFEST` performs source/hold/capacity revalidation and one atomic
reservation. Repeating that exact apply returns the existing run. An operation
ID with a different manifest is rejected. No public API or schema is added.

`--status OPERATION_ID --output REPORT` exports task state and request order.
`--verify OPERATION_ID --output REPORT` additionally checks Meta published state
and obtains the actual permalink without exposing Page credentials. A ledger
video ID alone does not prove a Post ID or publication.

All source reads use 63350 and verify `@@read_only=1`. This manually invoked CLI
uses the reserved operator connection pool, one connection at a time. It must
not be repurposed as a scheduled source collector. It reads the running sidecar
environment in process memory. Runtime manifests/reports/backups belong on the
mounted data disk; never commit secrets or runtime manifests to Git.

Before apply, back up the live SQLite database using the SQLite backup API.
Deploy only `core.py`, `manual_batch.py` and the CLI from the verified GitHub
commit, after comparing the live core baseline. Keep other deployed modules,
template settings and timers unchanged. Drain leases before the narrow sidecar
restart. The manifest hash, exact task identity and ordered task set are checked
again at claim; later tasks cannot claim while an earlier batch task is still
planned, preparing, ready or running. An accepted submission allows the next
Page to proceed while reconciliation confirms publication.

Ordinary runs retain their existing drama cooldown and selection blacklist.
All batches retain same-material reservations and unknown-result Page holds.
The root-only entry point is the trust boundary for approved exceptions; a
client cannot request these flags through the existing run-now API.

Rollback restores only the changed code and cancels only batch tasks with no
Graph attempt. Preserve the current SQLite, submissions, attempts, wrappers
and frozen recipes. Never restore the old database over new publishing facts.
