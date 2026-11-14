# Locked-account personal Excel cancellation

User selected recovery scope 1: old locked accounts' material can go to a NEW
healthy account; normal same-account duplicates remain fenced. Existing repeat
confirmation behavior is not expanded. No automatic repost or account remapping.

A dedicated per-minute SQLite maintenance pass cancels only personal Excel
children whose account has explicit platform-lock evidence or the exact verified
suspension error. Token failures, disabled switches and permission failures alone
are not enough. Terminal successful and failed histories remain immutable. Uploads,
reserved writes, unknown outcomes and relay work are retained. UPDATE/claim/log
writes serialize through BEGIN IMMEDIATE. Existing stopped-run guards reject
planning and posting after cancellation. Shared healthy pending siblings keep
material reservations; audited groups are revisited until every sibling settles.

Released active reservations permit ordinary Excel admission on a different
healthy account through its existing owner/language/media and pair-duplicate
checks. Same-account confirmed publication still requires the existing explicit
repeat confirmation. Original X receipts, account settings and target IDs remain
unchanged. Queue IDs and immutable batch/task identities are not deleted.

Deploy: scripts/deploy_x_video_account_recovery.py --directory PATH, from exact
pushed/fetched commit. Installs a new isolated timer without changing or restarting
live X sidecar/upload/pipeline units. Backup includes a consistent SQLite snapshot.
Rollback: systemctl disable --now x-video-account-recovery.timer; systemctl stop
x-video-account-recovery.service. Restore prior new-unit files/pointer if applicable.
Never restore old database snapshots over new receipts; cancellations stay audited
and can be reuploaded explicitly using a healthy target and future schedules.

Validation uses the actual runtime projection, because the live publishing lane
combines separately committed episode/import/transport overlays. Do not replace
its entire source tree from the base checkout. New modules are exact GitHub files.
