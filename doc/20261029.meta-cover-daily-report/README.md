# Meta cover daily report

The old notifier polled every minute and each scanner/worker completion could send changed batch totals. It now sends one daily report at 09:30 Asia/Shanghai for the preceding 24 hours, deduplicating actual outcomes by object and including older tasks completed during that window. The timer does not catch up at boot. First scheduled delivery after this release is October 30 at 09:30.

The scanner/worker completion callback is silent. Historical notice bodies, UUIDs and receipts remain intact. Each daily report freezes its data and body under `cover-daily-YYYYMMDD` and uses the existing durable `end` phase. A sent or uncertain notice is never blindly repeated. A definite failed delivery can be explicitly retried using its frozen body.

## Timing definitions

Every timing includes mean, nearest-rank P95 and sample count:

- Discovery/queue: completed scan metadata seconds, per batch.
- Queue to binding intent: includes queue, review waits, source preparation and Creative readiness, for successful Ads.
- New Creative claim to acceptance: includes governor waits, unsent deferrals and readback; excludes reuse hits.
- Binding intent to successful receipt: includes the binding request, verification and any later read-only reconciliation.
- Cleanup enqueue to verified deletion: includes queue, remaining references, account inventory and deletion.
- Ad enqueue to confirmed replacement: full elapsed latency of successful Ads.

These populations overlap and differ; they must not be added as a serial API profile. Existing history has no separate preflight/HTTP timing instrumentation, so those numbers remain unavailable. Current backlog/review/unknown state is explicitly a timestamped snapshot, separate from window outcome counts. The report queries only the local cover ledger in one read transaction, without a MySQL or Graph request.

## Validation and rollout

`python -m unittest discover -s tests -p 'test_cover*.py'` passed 125 tests. New cases cover the Beijing cutoff, half-open boundaries, result deduplication, old-batch completion, timing semantics, frozen delivery, unknown receipts, dry runs and removal of minute/boot triggers. Deployment must rerun the relevant tests and a live `cover.py --config <runtime>/config.json notify --dry-run`; preview must not issue media writes or send Feishu.

Fetch the exact GitHub commit into `<runtime>/releases/<sha>`. After scanner/worker are idle, run the existing `deploy/install_cover.py prepare <release> --active-ledgers-only` for consistent cover/shared-ledger backups and unit/config snapshots. Then run `deploy/install_cover_report.py apply <backup>`. This narrower installer leaves API, execution configuration and stopped-worker state alone. Verify next timer at 09:30, daily dry-run body, unchanged legacy deletion timer and unchanged execution counters.

Rollback: `python3 <release>/server-monitors/meta-violation-material-cleanup/deploy/install_cover_report.py rollback <backup>`. It restores the previous code link and notification units only; retain all latest ledgers and receipts. This re-enables the former frequent-notification behavior. Live paths and receipt details belong in the deployment audit.
