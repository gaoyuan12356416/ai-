# Social report reliability repair — 2026-10-29

Scope: the existing cookie-authenticated social report, UTC attribution and integer metric semantics retained. No publisher/Graph write, Feishu send, permission change, or source-table write.

## Evidence

- Screenshot-equivalent YouTube query: UTC 2026-10-01..28, tenant screenshot actor resolved from the existing administrator directory, 2,100 exact and legacy campaign IDs.
- Raw source coverage contains all 28 dates. Original serial report completed in 31.312 s; each weekly SQL took up to 7.705 s including gated process/connection overhead, close to the 8 s statement cap under load. Existing logs also contain closed-client/double-response BrokenPipeError traces.
- Fixed cold report: 22.225 s; warm: 2.073 s. Every total matches exactly: revenue $18,630.87; clicks 75,837; views 104,861; installs 10,219; conversions 1,374; refunds $113.58; publications 563; links 1,061.
- X scope has 4,712 historical IDs. Former 100-ID range reads require 48 SQL subprocesses per range; web time budget is 40 s. Site/date batches of at most 3,000 IDs remove that deterministic connection-overhead failure while retaining exact-case/site/date checks.
- The FB attribution collector crashed with SIGBUS at 19:59:33 and automatically recovered at 20:00. Ledger quick_check is `ok`. Code reopened/closed WAL/SHM descriptors while its SQLite connection was alive, which can cancel POSIX advisory locks. The crash mechanism is inferred, not proven from a core dump. Eliminate the unsafe descriptor operations while retaining private file permissions and the collector lock. Regression verifies a second process remains blocked by the original process lock after permission tightening.

## Changes

- Source SQL cache: process-local, exact SQL + reader + row-budget key; 300 s TTL, 32 entries / 32 MiB payload cap, at most 16 distinct pending reads. Coalesce duplicate reads, copy results, do not cache errors. Attribution and tenant/owner ACL still run for each report.
- Broad YouTube/X ranges use at most two gated read windows concurrently for HTTP, unchanged 40 s collection deadline. The bounded concurrent windows permit a 12 s statement cap; scheduled collector overrides remain intact. Small scopes and single-day queries retain their existing indexed path.
- Any failed batch still fails that source closed; rows never turn into zero or partial successful source data. Query failures are `unverified_dates`, distinct from actually absent `missing_dates`.
- Browser disconnects do not attempt a second HTTP response. Internal diagnostics log query hashes and error classes/codes, never SQL parameters/passwords.
- Preserve the upstream frozen daily catalog capability behind `_system_report`; browser actors cannot supply it.
- FB sidecar permissions are changed with metadata operations, never a data-FD open/close while SQLite owns the file. Where nofollow chmod is unavailable, require a caller-owned parent not writable by other users; reject symlinks/nonregular files before path-based chmod.

## Verification and deployment

Related Python tests and the isolated browser suite must pass before deployment. The browser harness now expects the currently deployed title 社媒效果分析. Actual frozen-source totals and query times are recorded above. Missing-source, concurrent read, failed batch, ACL and integer reconciliation regressions remain enabled.

Push the repair branch, fetch that exact commit, then `python3 ops/social-report-reliability-20261029/deploy.py --commit <fetched-commit>`.

The deployer checks all live preimage hashes, backs up every modified/new path, changes only report modules + two static files, and makes a new collector code release by overlaying the single changed FB module onto the previous maintained collector release. No timer is disabled or rewritten. Restart the main API with its existing safe restart helper, then start/observe a read-only attribution collector run.

Rollback: `python3 ops/social-report-reliability-20261029/rollback.py`. Preimages and original collector link are in `/mnt/data-disk/social-report-repair-20261029/deploy-backup/deployment.json`. Rollback restores code/static files and collector link, then safely restarts the API. Existing publisher state, report ledger, timers and source data remain in place.

SQLite lock rationale: https://www.sqlite.org/howtocorrupt.html#posix_advisory_locks_canceled_by_a_separate_thread_doing_close_

## Facebook follow-up validation

The deployed all-channel weekly check revealed an independent MySQL 3024 timeout on legacy Facebook site 2049. The frozen candidate list contained 15,772 exact campaign/Page/material tuples. Adding prefilters or changing tuple encoding did not reliably solve the latency, so the index/encoding experiments were not deployed.

The follow-up instead reads bounded exact Page candidates grouped by all three binary IDs within disjoint, at-most-four-day windows, then checks exact campaign/Page/material tuple membership using a Python set before returning any metric, with at most two concurrent gated reads and the unchanged shared 40 s deadline. Any failed window fails the whole source. The combined 50,000 metric-row cap is checked across windows; coverage sentinels retain dates independently of content traffic. Internal scheduled/global queries keep the existing path.

229 Python regressions pass (including partial-window failure, combined cap and source-proof checks); 72 browser checks pass. Cold UTC Oct 22–28 all-channel report completes in 39.006 s, all three channels complete, zero missing or unverified dates. The screenshot YouTube month is already complete with exact pre-change totals. All 9,748 Facebook legacy source rows (including coverage sentinels) match the original exact-tuple query byte-for-byte after normalization. Cross-tuple, case-collision and pre-filter candidate-overflow tests protect the source boundary.

For the narrow follow-up, push and fetch the exact revision then run `python3 ops/social-report-reliability-20261029/deploy_followup.py --commit <fetched-commit>` and the API's existing safe restart helper. Only `features/social_analytics/backend_scope.py` changes live; the collector and its schedule stay unchanged. Follow-up preimages are in `/mnt/data-disk/social-report-repair-20261029/deploy-backup-followup/deployment.json`. The updated `rollback.py` restores both stages before a single safe API restart.
