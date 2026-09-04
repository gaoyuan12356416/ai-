# Drama reservation languages and operator-confirmed unlock recovery

The available-drama endpoint selected episodes using each account's language,
but transactional reservation omitted that map and defaulted every account to
English. A valid non-English candidate could therefore roll back an entire
batch. Reservation now receives trusted language metadata for the full frozen
account scope, including accounts omitted during candidate-local preflight.
Affinity, episode order, partial-capacity selection and unknown-outcome fences
remain enforced.

The recovery CLI supports one operator-confirmed unlock for a frozen drama
Repost that failed explicitly with a locked-account HTTP 403. It requires exact
target and source Post identities, an already confirmed source, unchanged drama
ownership and progress, exactly one source attempt and one failed target attempt,
and no ambiguous outcomes. It records the original failure in an append-only
audit before rearming only the target Repost. It never calls X, creates a queue,
resets an attempt counter, or recreates a source Post. A second recovery is
rejected. Actual dispatch uses the existing running Sidecar and its account
locks, identity checks and durable Repost state machine.

Validation: 132 store/schedule tests and 180 runner, language-routing, relay and
OAuth tests passed offline. New coverage includes mixed languages with affinity,
language drift, one-time recovery, identity mismatches, ambiguous outcomes,
preserved source counters, progress advancement and original-failure audit.

Deploy from a verified commit after backing up live state and draining active
publishing. Match both runtimes' shared store module while preserving unrelated
runtime differences. Rehearse the recovery against a database backup first.
Use the CLI's default read-only mode before an explicitly authorized apply.
Restart only affected services and verify health, code hashes and ledger state.
Rollback code only; retain all current publication history, token state and
recovery audit. Environment-specific operational evidence stays outside Git.
