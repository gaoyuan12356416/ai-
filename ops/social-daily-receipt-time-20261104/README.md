# Late Facebook receipt reconciliation must not block the income report

The legacy task has a dedicated `completed_at_utc`. Its matching publish ledger
has only `updated_at_utc`, which changes during receipt reconciliation. Comparing
their UTC dates misclassified confirmed posts as attribution conflicts and blocked
the entire 11:00 daily report before any delivery batch was created.

Use the task completion timestamp after validating the matching task, Page,
material, Post ID, published status and absence of unknown outcomes. Missing or
invalid completion times remain errors. A later change to an already frozen
publication date or object remains a conflict.

Recover only existing `publication_time_mismatch` records with frozen ownership,
no prior publication fact, identical frozen/last-observed/current evidence, and a
newly validated confirmed publication. Other conflicts stay held. Preserve all
existing identity evidence, original completion dates, publisher databases,
historical report files and delivery receipts.

## Validation and deployment

Run `python -m unittest discover -s tests -p test_social_facebook_attribution.py`
and the social daily-report, billing and backend-scope suites. On CPU
`43.166.187.96`, clone the pushed branch to
`/mnt/data-disk/social-daily-receipt-time-20261104/source` and verify its commit.

Run this directory's `deploy.py stage`, then `deploy.py canary`. Staging copies
the existing independent daily release and changes only `facebook.py`. The canary
uses an online backup of the attribution database, verifies unchanged ownership
and existing publication facts, and checks that historical reports did not change.
Run an isolated daily `--preview` with `--facebook-attribution-db` pointing at the
canary and verify `require_complete(report)` before activation. If the snapshot
ages past five minutes, rerun the canary collector before previewing.

Run `deploy.py apply`; it backs up attribution and delivery state, holds the
existing reporting/collector locks and atomically switches the release link.
Start `social-facebook-attribution.service` and verify both sources are complete.
Then start `youtube-publisher-daily-report.service` for the authorized missed
report, using its existing delivery ledger. Verify the receipt with Feishu GET.
The existing 11:00 timer is unchanged; no publisher service restart is needed.

## Rollback

`python3 /mnt/data-disk/social-daily-receipt-time-20261104/source/ops/social-daily-receipt-time-20261104/deploy.py rollback`

This switches code back without restoring any database or sending any message.
The prior code can recreate the timestamp conflicts, so rollback is an emergency
code fallback, not a durable resolution. Keep the preserved release and snapshots
under `/mnt/data-disk/social-daily-receipt-time-20261104/` for audit.
