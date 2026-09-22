# X automatic schedule account failure isolation

Material and drama schedules retain their frozen full account scope for reporting,
but skip an individual account on explicit HTTP 409 authorization, approval,
identity, disabled-account, or existing ledger-hold rejection. Healthy accounts
continue in configured order. Short-lived Access Token expiry alone is not a hold.

Before queue reservation, if a candidate account becomes unavailable during media
preflight, regenerate candidates and FIFO/language proofs for the smaller scope.
Each repeat removes at least one account; no failed account is retried in that run.
No healthy candidates means failed_preflight without a queue or publish attempt.
Persisted authorization/approval holds participate in the co-located account/post
store's FIFO capacity calculation and transactional write fence. Legacy post-only
stores keep their existing ledger-only hold source.

Premium relay lookup with refresh=False now actually uses the current snapshot;
refresh=True still verifies through the account service. This avoids an unrelated
network refresh changing another target during atomic plan preparation. Every real
source/target publication retains its existing fresh identity/entitlement guard.

Frozen queues are never reassigned or replayed. Unknown outcomes, identity-order
mismatches, transaction failures, shared HTTP 429/5xx and malformed responses keep
their fail-closed behavior. Existing account lock/unknown-write evidence takes
precedence over an authorization hold. No schema migration or historical recovery.

## Validation

Offline suites: test_x_post_schedule_runner, test_x_post_multi_schedule_store,
test_x_accounts, test_x_post_material_random_relay,
test_x_post_premium_relay_repost, test_x_account_language_routing.
Covered both pools, isolated revoked/disabled/unapproved/missing-token accounts,
mid-preflight scope rebuilding, all-blocked zero-write behavior, FIFO proofs,
expired-refreshable accounts, reauthorization, unknown outcomes and relay refresh.

## Deployment

The live release is a deliberate composite. Overlay only the three changed runtime
files on a copy of the verified live release: scripts/x_post_schedule_runner.py,
features/x_accounts/oauth_service.py, features/x_posts/account_blockers.py.
Also update the main API's account_blockers.py. Preserve every other live file.
Fetch the exact commit from GitHub, compare pre-change hashes, run the suites on the
staged composite before switching. Stop active X timers, drain existing oneshots,
hold the shared runner lock, back up SQLite and changed files, record token hashes
without exposing tokens, and switch the current symlink. Restart only the X sidecar,
X Auto sidecar and main API, verify health, then restore prior timer states. Do not
run a publisher manually to test deployment.

Rollback under the same drained-runner lock: restore the previous current symlink
and main API account_blockers.py backup, restart the same services, verify health,
and restore timers. Preserve the current database, tokens and publish ledger.
