# Cases

1. Exhausted temporary refresh yields zero native upload attempts and no native operation.
2. Temporary identity failure recovers once; exactly one upload intent.
3. Revoked refresh is never retried.
4. Crashed issued upload stays unknown.
5. Explicit native retry requeues the same job once, retains previous generation and approved descriptor.
6. Unknown/written tasks cannot be re-admitted.
7. Queued DTO shows waiting; failures remain visible.
8. Preserve node capability exclusions, configuration, active claim generations and reservations at deployment.
9. Operational recovery validates original task identity, zero video/session, source/cover hashes, node acknowledged no-write receipts, and no material duplicates before changes.
