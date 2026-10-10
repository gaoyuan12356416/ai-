# Explicit recovery of frozen drama upload-credit failures

The operator authorized resuming an exact historical set of drama episodes whose
media-finalize request explicitly returned HTTP 402 `credits depleted`. The tool
accepts only failed logs with one original attempt, no media/Post identity, no
unknown outcome and matching frozen queue, pool, binding, language, route and
episode progress. The full manifest remains private operational evidence.

`scripts/recover_x_drama_credit_failures.py` is a one-time maintenance command,
not a change to automatic retry policy. A SHA-256 pinned manifest and explicit
`--apply`, actor and source commit are required. First run without `--apply`.
The command waits for the shared publisher lock, takes a checked online SQLite
backup, re-verifies each account through the running sidecar, appends immutable
original evidence, and rearms just that original queue/log immediately before
its guarded publish request. Lifetime attempt counters, frozen attribution and
all original IDs/content/route/bindings remain unchanged. Only a confirmed final
Post or target Repost advances the drama and clears its error.

Published or ambiguous outcomes can never be reset. One recovery audit per queue
prevents consuming the same operator decision twice. Rate limits, ambiguity and
renewed credit depletion stop remaining retries. An explicit account suspension
or lost long-video membership leaves that row unchanged and does not hold usable
accounts. A frozen route is never replaced to work around expired membership.

Renew the original schedule execution lease in the rearm transaction. Otherwise
the minute claim poller treats a historical running batch's old lease as stale
and applies pool review holds while an authorized publish is still active.
Proven credit-failure holds with an exact `x_post_schedule_stale_claim` run are
reconciled through a separate immutable audit before account verification. Their
original failure remains recorded, and actual unknown publication fences remain
authoritative. This recovery does not stop or modify any normal timer.

The live sidecar, its media fingerprint/duration and entitlement checks, token
rotation ownership, attribution wrapper and durable X receipt path are reused.
No source database writes, new queue, service restart, schedule edit or token-file
access is needed. Deploy only this script from the pushed GitHub commit into an
independent maintenance directory and import the verified current runtime.

Validate with `python scripts/test_x_drama_credit_recovery.py`, compile the script,
and rehearse arming on an online backup using the production schema and existing
`XPostStore._sync_run`. Inspect final target receipt IDs/URLs, cumulative attempts,
drama progress, frozen fingerprints and audit before/after evidence.

Rollback: stop scheduling this one-time command; do not terminate an accepted X
request. Never restore the database backup over confirmed/ambiguous remote
outcomes. Any armed but unattempted row requires exact audited reconciliation
before rollback. Code removal does not delete the additive audit table or ledger.
