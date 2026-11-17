# AI storage separation

New AI outputs use a dedicated Hong Kong COS bucket carrying `team=ads_ai`.
Shared advertising and SocialKit buckets keep their original ownership and data.
TikTok retains its already verified delivery origin. Only the new `/ads-ai/` CDN
path routes to the independent bucket; historical URLs keep their existing origin.
TT/FB delivery prefixes and cloud delivery prefixes use this namespace.

The production install uses narrow patches because independently deployed modules
share the live runtime. `install_overlay.py` checks patch context, compiles staged
files, verifies no concurrent edit, and backs up each file before replacement.
It never restarts services or changes queues.

Set `COS_LEGACY_BUCKET_DOMAINS` to the explicit old bucket-to-host map. A resumable
upload with an existing checkpoint keeps its recorded bucket; new uploads use the
new default. Completed GPU receipts still require the exact bucket, URL, checksum,
size, ETag and upload binding. Historical material-source URLs remain accepted only
for the configured old domains.

For the cloud preparation node, capture outstanding `work.identity` values after
graceful stop and set `legacy_work_ids` plus `legacy_storage` (old bucket, queue,
prefix and delivery configuration). Only those identities use the old executor.
New work uses the new bucket and queue. The renewable STS session grants the exact
old/new prefixes while legacy work remains; no action ledger or receipt is reset.

Update producer bucket/domain configuration and append the new exact host to
existing consumer host lists. Retain the old hosts. Activate consumers before
producers, use each worker's normal graceful drain, and preserve active dependent
planner/timer state via `publish_scheduler_health.py` for affected parent owners.

Verification: storage compatibility unit tests, live credential upload/readback,
one-second CI transcode in the new bucket, effective process configuration,
historical URL reads, new object evidence, and scheduler restoration.

Rollback future writes only, after the same graceful drain. Keep compatibility
code, both allowed bucket domains, new objects and all ledgers. Restore original
producer destinations from the saved environment files or remove their storage
only overrides, while retaining reader host extensions. Extend
COS_LEGACY_BUCKET_DOMAINS to include both buckets, so either checkpoint can resume.
For the cloud node, preserve the current new-bucket work IDs as legacy IDs with
the new storage/queue configuration, and make the original shared storage the
primary configuration; remaining pre-cutover work then uses that original target.
Renew the scoped session and start the same affected services and their previously
active scheduler dependencies. Never replace cloud configuration blindly while
paid action identities remain bound to the new bucket.

The default new Tencent CI queue has ten concurrent slots. Matching a previously
raised quota requires a Tencent support request; do not claim that a queue name
or routing update raises this quota. The verified TikTok CDN domain remains shared
and retains its original billing attribution.
