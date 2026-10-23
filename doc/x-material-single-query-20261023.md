# Exact material IDs: one gated database request

The previous source-ID lookup opened three mysql clients sequentially: source
record, drama resources and deployment information. ID 7424637 used PRIMARY,
content_id and ac indexes, but the complete read took 6.8144 seconds while the
individual SQL statements took approximately 1.6, 4.1 and 2.0 milliseconds.

Exact ID searches and explicit selected-ID revalidation now use one SELECT with
three UNION ALL branches through the existing FIFO-gated read-only adapter.
Source IDs remain bound parameters. The mapping branches start from at most 200
distinct source content IDs and retain their existing indexes, app 1479 scope,
independent 4001-row overflow detection, complete mapping rows and validation.
Keeping the branches separate avoids multiplying resource/deployment rows.
There is one client, one gate admission and one database connection per lookup.

Browse and text/title search keep their existing query paths. No source cache,
schema/index mutation, SQL timeout increase, gate bypass or publishing operation
is added. Explicit-ID reads retain cross-product behavior, language/cursor
filtering and bounded missing-duration recovery. Preflight continues to reread
current data through the same combined path.

Local validation: 113 personal-publishing backend tests passed, including
one-request behavior, binding, pagination/filtering, conflict and overflow
rejection, metadata recovery, null/Unicode round trips and malformed result
rejection. Git diff whitespace check passed.

Deploy only features/x_video_publish/sources.py with the guarded installer
scripts/deploy_x_material_single_query.py, based on production 37de345.
The installer snapshots affected code/SQLite/tokens, drains workers, checks live
file hashes, restores original service and trigger states, and verifies ledger
and token fingerprints. Its rollback restores only the preceding code file.

Production measurements and the exact rollback command will be appended after
candidate equivalence checks and deployment.
