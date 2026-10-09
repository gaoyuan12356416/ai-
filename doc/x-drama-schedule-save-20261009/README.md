# Unavailable drama owners no longer prevent schedule edits

Saving an enabled drama schedule formerly required every unfinished drama owner,
including explicitly suspended or disabled accounts, to remain selected. Those
accounts also failed publish-eligibility validation, preventing an operator from
saving a healthy account subset. The missing client/API error mapping displayed
that business conflict as service unavailability.

The save guard and assignment selector now disregard an omitted owner only when
the existing persisted account blocker reports `x_account_not_publishable`.
Unrecognized/missing authorization evidence, refreshable Access Token age, active
owners and unknown-write/ledger holds retain the strict binding protection.
Inspect all omitted owners so an unavailable owner cannot hide a healthy one.
Do not unbind, reassign, advance, retry, or alter the historical drama/queue/log
records. A restored account must be explicitly readded to the schedule.

The client and main API preserve HTTP 409 for unfinished-owner and slot-in-progress
conflicts. The unfinished-owner message includes its actual drama and account ID.

Local regression: 328 tests passed across multi-schedule store, OAuth/account,
API mapping and scheduler modules. Additional coverage includes normal accounts,
known unavailable states, missing/unrecognized state, unknown writes, omitted
healthy owners behind unavailable owners, selector continuity and ledger equality.

Deployment uses `scripts/install_x_drama_schedule_save.py --commit <full SHA>`.
It fetches the exact pushed GitHub commit, preserves the verified live composite,
runs Linux regression, backs up code and SQLite on the verified data disk, drains
active Auto work, restarts only Sidecar/API, reads back hashes and health, and
restores previously active Auto timers in `finally` with a separate rescue timer.
It preserves schedule/manual timer state and does not save a new schedule or
publish/catch up historical posts as deployment verification.

Rollback: use the deployment manifest under
`/mnt/data-disk/x-post-automation/maintenance/20261009-drama-save-<SHA8>/`.
Drain active Auto requests, restore its `main/` files to `/root/drama_material_service`,
atomically restore `/opt/x-post-automation/current` to `previous_release`, restart
`x-post-automation.service` and `drama-material-api.service`, verify both readiness
endpoints, and restore only the timers that were active before rollback. Preserve
the live SQLite databases and tokens; the database snapshot is audit evidence.

Exact rollback command (substitute the recorded release/manifest from deployment):
`python3 <release>/scripts/rollback_x_drama_schedule_save.py --manifest <backup>/manifest.json`.
`scripts/audit_x_drama_schedule_save.py` performs a read-only live SQLite snapshot,
replays the 17-account/one-random-batch edit in shared in-memory SQLite, verifies
candidate selection, and compares all historical bindings/account/queue/log
fingerprints. It neither submits a production save nor calls X.
