# X account revenue attribution by campaign ID

The account-list revenue collector previously matched the complete campaign
name and required a confirmed publication. A changed campaign name or an
unconfirmed X result could leave a bill unallocated despite an exact frozen
campaign ID identifying its target account.

## Revenue contract

- Read `kunlunads_dev.ads_drama_bills` on the read-only endpoint, with fixed
  `site_id='2116'`. Keep USD sums and Beijing event-time yesterday unchanged.
- Aggregate bills once per case-sensitive exact `campaign_id`. Join this key
  to the single frozen `af_c_id` in `x_post_publish_log.long_url`, then to
  `x_post_queue` through `log.queue_id=queue.id`.
- Require `af_c_id=str(queue.id)` and matching log/queue account IDs. Attribute
  revenue to the frozen target account, including Premium relay deliveries.
- Publication status does not decide bill ownership. A bill associated with an
  unknown publication may receive account attribution while the publication
  remains unknown, blocked from retry, and absent from confirmed Post counts.
- Reject missing, duplicate, inconsistent or ambiguous identity evidence.
  Unmatched bills remain unallocated; campaign names never provide a fallback.
- Preserve confirmed Post/Repost counting, publisher state, queues, tokens and
  scheduled publication behavior.
- The existing schema-1 cache stays compatible with the API. Attribution
  metadata identifies `campaign-id-publish-ledger-v2`.

## Validation and deployment

Run `python -m unittest scripts.test_x_account_operating_stats`, compile the
module/refresh script, and run `git diff --check`. Replay the source aggregates
against the live read-only ledger and reconcile allocated plus unallocated
money to the complete source total before changing the cache.

Push the exact commit to GitHub. Fetch that commit into a separate server
release, test it there, and back up both the current module and JSON cache on
the verified data disk. Deploy only `features/x_account_stats/service.py`, then
start the read-only `x-account-operating-stats.service` oneshot and read back
the cache and account DTO. Keep the normal 10:00/11:00/12:00 timer unchanged.
No publisher, OAuth sidecar or main API restart is required for this compatible
cache update.

## Rollback

With the statistics refresh inactive, restore the backed-up module and run the
read-only refresh oneshot again. If a refresh cannot complete, restore the
backed-up cache atomically. Preserve current X ledgers and token state. Record
the exact commit, backup path and commands in private deployment evidence.
