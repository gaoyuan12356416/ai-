# Unified ordinary and profile Post logs

The existing admin GET /api/admin/x-posts/logs now reads ordinary and profile
SQLite ledgers in a single read-only connection/snapshot, with each join keyed
by lane plus ID. Matching numeric queue/log/run IDs cannot cross-link receipts.
Filters are shared, including auto_publish. Results sort by queue creation time,
then lane and queue ID, and paginate/count AFTER the union. No whole-ledger export
or per-page concatenation is performed. Missing profile data fails the request
rather than falsely claiming a complete old-lane-only list.

The task source cell prefixes profile records with 画像. Existing admin gate,
read-only behavior and ordinary transactional controls remain. Profile actions
are disabled here because their dispatcher differs; no ordinary reset/stop is
issued against a profile row. No publishing lane or source schedule changes.

GitHub baseline captures the actual deployed log modules. Deploy only four
reviewed files: features/x_posts/log_query.py, log_management.py and static
x-post-logs.html/js. Back up originals, verify pre-change hashes and fetch exact
pushed revision. Restart only drama-material-api.service, never X publishing or
pipeline services. Database schemas and business records are not modified.

Validation: 9 two-ledger regressions (including deliberately colliding IDs,
filters, pagination, missing data, admin gate, no business DML) and 10 existing
ordinary control tests. Real DB unified first page/date queries are read-only.
A generic imported NativeTests fixture has an unrelated baseline account-fence
failure; direct Controls and UnifiedLogTests are the scoped regression suites.
