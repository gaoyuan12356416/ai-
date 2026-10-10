# Current pool one-time publication

Authorization: the operator requested “当前的素材池和短剧池执行一次发布”.

The helper `scripts/x_post_current_pools_once.py` creates one audited claim per
source using the current saved account order, body template and version. The
request ID is durable and idempotent. It does not alter random daily plans,
re-arm historical failures, or change pool/queue deduplication. The existing
deployed scheduler performs account checks, language routing, FIFO selection,
media preparation, atomic queue reservation and sequential publishing through
the running Sidecar. Only the two exact claims are passed to the runner.

Run under the shared runner lock and the existing schedule environment. The
operator process requires database access; OAuth refresh and X writes remain
inside the existing service. A SQLite online backup is saved before claims.

Validation: four tests cover frozen current scope versus next-day plan scope,
idempotent replay, atomic rollback on config drift, and natural-slot collision.
The runtime scheduler/service/OAuth hashes match this checkout after newline
normalization. No application restart or recurring timer change is needed.

Rollback boundary: before execution remove the standalone helper to abandon the
operation. After execution preserve claims, queues, logs and confirmed external
posts; never restore the pre-run database over live publication history.
The original daily configuration is unchanged.
