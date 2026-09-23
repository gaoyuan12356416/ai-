# Design review

Accepted: independent manual-link storage instead of fake publication tasks; global short-link ID allocator avoids filesystem collisions. Existing publish-attribution validation remains untouched. Exact actor scope on status lookup includes admins. POST double-checks channel eligibility and drama metadata before reserving an ID. Readback/retry of already-published operations returns frozen data.

Resolved: full-catalog open/group-by would scan episode rows; use indexed prefix/exact-ID search with bounded results. Missing/conflicting mappings fail before reservation. Deployment transforms only the reviewed route anchor in live app.py and checks all expected hashes.
