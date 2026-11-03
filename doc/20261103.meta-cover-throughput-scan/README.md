# Cover account throttling and scan connection reuse

The cover governor used the highest recent account usage to halve the shared
execution limit on every response, including responses from unrelated healthy
accounts. One account at 70% could therefore collapse all 32 slots to one. At
that capacity source preparation degenerated into repeated one/two-row SQL
snapshots and prefetched contexts expired while waiting for account cooldowns.

Cover execution now enables account-scoped usage handling. A BUC header is local
only when its top-level key is exactly the requested Ad Account; mixed or
unrecognized scopes remain global. Account usage at 70% reduces that account to
one slot; fresh feedback below 60% restores its existing two-slot ceiling. At
85% the existing account cooldown still applies, including Meta's estimated
regain time. Application pressure and explicit rate-limit errors retain global
reduction/cooldown. The global 32-request concurrency ceiling, 24 request starts
per second, source freshness and mutation guards are unchanged. Legacy callers
retain the previous governor behavior by default.

The scheduler skips cooling accounts before source preparation and dispatch,
allowing other accounts to progress without consuming stale prepared contexts.
Metrics distinguish global usage, account throttling and skipped cooling
accounts.

Account cooldown also yields an already-started cover task before its next HTTP
request. Otherwise a single post-bind readback waiting for a long account
cooldown holds the end of a 256-task cycle and stalls unrelated queued accounts.
Unsent writes defer; sent writes retain their receipt and become read-only
reconciliation candidates. Cooldown during reconciliation remains unresolved
without pausing all workers. No request is sent before the cooldown expires.

When the backlog concentrates in a few accounts, unused global slots do not
mean those accounts have spare execution capacity. The scheduler consumes its
existing prepared buffer before refilling it, unless a ready account without a
buffered context can use a free slot immediately. This prevents single-task SQL
top-ups while retaining overlap, the per-account limit and the unchanged
30-second source freshness checks. A deterministic staggered-completion test
covers two accounts using only four of the 32 global slots.

## Scan repair

Live diagnostics reproduced MySQL 2013 during session initialization. The old
reverse lookup opened a new gated connection for each indexed range. Cover
scans now reuse one read-only snapshot for trigger evidence and one for the
complete material reverse lookup, retaining the existing bounded per-query and
session timeouts. Enrichment of new candidates remains separately bounded.
Every range and pagination completion check is preserved; no historical Ad is
excluded by an added publication-date filter.

A transient connection failure may restart the whole scan once in a fresh
batch, after closing and marking the original batch failed. Only proven unsent
rows of failed scan batches can be rehomed by the existing queue protocol.
Partial pages are never merged across failed snapshots, and no media mutation
is replayed. SQL syntax/data errors and statement execution limits are not
retried. Redacted SQL diagnostics now default to the runtime diagnostics
directory, and batch failures retain diagnostic IDs and MySQL error numbers.

## Validation and rollout

Run the entire module test suite. Targeted regressions cover repeated account
pressure without suppressing other accounts, account/app cooldowns, conservative
handling of unrecognized BUC scopes, skipping cooling accounts before SQL, and
complete-scan restart after a partial batch with no replay or execution of the
failed batch.

Use the existing narrow `deploy/install_cover_worker.py prepare/apply` workflow
after GitHub commit/push and exact-SHA checkout. Stop scan/notify timers and drain
the services before the backup/switch. Preserve current ledgers and unknown
outcomes, the four-hour report card and the disabled legacy deletion timer.
Validate real worker cycles and a complete scan before claiming the scan fixed.
Compare processed tasks per active-cycle wall time, SQL preparation counts,
new failures/unknowns and source scan metrics; do not equate waiting-review rows
with successful replacements or claim identical workloads across scan times.

Rollback uses this release's worker installer and its backup directory after
stopping/draining the cover services. It preserves all latest receipts and
queues, restores prior code/units and leaves execution stopped.
