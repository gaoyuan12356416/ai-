---
name: meta-violation-material-cleanup
description: Maintain the hourly Meta violation-material cover replacement and one-attempt old Creative cleanup for DramaWave and HotDrama on CPU 43.166.187.96, including durable queues, shared object locks, and Feishu summaries to 郜远. Do not use the disabled legacy Ad deletion workflow for this contract.
---

# Meta violation-material cover replacement

## Current contract (2026-10-28)

The approved cover replacement plan supersedes the old five-plus-paused deletion contract. Preserve old code, task history, credentials, receipts and unknown fences, but keep meta-violation-material-cleanup.timer disabled. Do not run the old live run.py deletion entry point for this workflow.

Source: GitHub gaoyuan12356416/ai-, server-monitors/meta-violation-material-cleanup. Runtime: /mnt/data-disk/meta-violation-material-cleanup. current points to an exact GitHub release under releases/<sha>. Read [requirements](../../../../doc/20261028.meta-cover-replace/requirements.md), [API](../../../../doc/20261028.meta-cover-replace/api-doc.md) and [deployment](../../../../doc/20261028.meta-cover-replace/deploy.md). Older OPERATIONS.md/deployment-v3.md describe historical operations, not authorization to reactivate deletion.

## Selection and transformation

- Roots 1479 (DramaWave), 3302 (HotDrama) and their direct product children only.
- Exact seven policies: Adult Nudity and Sexual Activity; Adult Sexual Solicitation and Sexually Explicit Language; Adult Sexual Exploitation; Human Exploitation; Violence & Graphic Content; Spam; Drugs and Pharmaceuticals.
- Scan the preceding three-day half-open window each hour. At least three distinct violating Ad IDs per attributable canonical material trigger; duplicate policies on one Ad count once.
- Include current already-qualified materials on the first scan. Persist triggers and waiting tasks beyond the rolling window. Each hour discover newly associated Ads; exclude queued/terminal pairs and proven historical Ad deletion receipts before expensive reads.
- Replace only nondeleted Ads currently free of review issues. Review/appeal waits are durable and checked hourly. Current source identity, account, credentials and Meta binding must agree before writes.
- Fetch ads_drama_resource.cover by root product, drama and language. Use object_story_spec.link_data.picture directly; never download/upload an image as an intermediate step.
- Source type must be drama (data_source=6). Preserve existing HotDrama reuse of DramaWave materials; always resolve the cover using the Ad's root product, exact shared content ID and language.
- Preserve observed creative_sourcing_spec.destination_screenshot_spec OPT_OUT and page-equal object_id; unsupported media sourcing or a different object_id remains a per-Ad failure.
- The creation wire projection omits read-only enable_direct_install=False and Meta's retired standard_enhancements aggregate only when individual feature settings exist. Keep the full expected payload/hash and strict readback; preserve every individual setting except the explicit 2026-10-28 approved absence of video_auto_crop after conversion to an image. Keep submitting its original value; accept only its absence on a verified single-image Creative, never a changed value or missing other feature. Do not reset prior failures to test a new release.
- Preserve the full destination including query parameters, copy, CTA, deep links, attribution and identity fields. Unsupported meaningful parameters, ambiguous covers or multi-drama conversions fail explicitly; do not invent defaults or drop fields.
- Readback may accept only the observed exact administrative Creative name suffix (original name plus date and 32 lowercase hex digits), never a changed ad headline. Permit missing retired standard_enhancements only when creation_payload validated its omission; preserve all individual features and verify any returned aggregate.
- Binding readback may normalize only the three derived post tracking actions using verified old/new Creative effective_object_story_id with the same Page. Compare the entire tracking multiset including duplicates; Pixel, conversion IDs, extra fields and action counts remain exact. Unknown bindings require fresh Ad and Creative GET plus full payload verification before confirming; never resend the binding.
- effective_object_story_id can be absent until binding. Refresh the new Creative after the accepted binding and verify its payload before comparing derived post tracking. A still-unavailable post identity is unknown awaiting GET-only reconciliation, not permission to replay a binding.
- Reuse only matching account/full payload/cover version. Without reliable cover version, scope reuse to the batch.

## Write and deletion boundary

Create/reuse a cover Creative, verify it, update only the original Ad's creative field, and read back the binding and unchanged parameters. Only confirmed successful replacement queues the old Creative; success and cleanup admission commit atomically. A created Creative with failed binding does not authorize old Creative deletion.

Deduplicate cleanup by account and old Creative. Finish related replacements, check known references hourly, then merge complete account inventories covering all nondeleted Ads including archived. Any remaining reference, unreadable page, unknown outcome or unresolved appeal blocks deletion. Never expand Ad modification scope solely to clear references.

Hold shared object reservations through final reference verification and deletion. Commit the shared and local once-delete intent before sending. This workflow sends at most one DELETE per old Creative across Ads/batches/restarts. Historical success receipts are reusable; unknown fences take precedence. An unrelated old definite failure does not consume this newly approved post-replacement attempt.

Definite failure is not automatically retried. Unknown writes are reconciled read-only; missing objects or permission errors do not prove deletion. A known-ID unknown creation may be accepted after exact ID/account and payload verification, allowing only the first unsent Ad binding; never replay CREATE. Deletion failure does not undo a successful cover replacement.

## Concurrency and storage

Use 32 execution workers. All GET/POST/DELETE share Graph initial/max in-flight 32, per-account 2, total request starts 24/second; respect platform cooldowns and reduce later concurrency. SQL/FIFO preparation stays on the owner thread; release SQL before Graph waits. Use read-only FIFO Gate with SQL_GATE_BYPASS=0 and verified @@read_only=1. Never bypass through direct drivers or mysql.real.

cover.sqlite3 is separate from /mnt/data-disk/fb-ad-asset-delete/tasks.sqlite3. Preserve both. New shared reservations/once guards are additive and visible to existing deletion claim_object. Do not fabricate old jobs or import their backlog. Only an exclusive worker process lock plus proof the previous worker exited permits recovery. Sent intents become unknown; provably unsent work may be prepared again. Never clear unknown fences to force replay.

## Commands and units

From this release's module directory, use cover.py --config <runtime>/config.json with:
- preview --max-ads 256: isolated preview ledger and read-only Graph.
- scan: source discovery and durable queue only.
- work --once --max-ads 256: approved natural-hit first cohort.
- work: continuous worker; requires cover_live_enabled=true.
- status: read counters without recovering tasks.
- notify: notification recovery only.

New units: meta-cover-scan.service/.timer, meta-cover-worker.service, meta-cover-notify.service/.timer. Scan timer runs every hour. Do not assume a unit is enabled from its file; verify systemctl state. cover-write-paused.json is durable stop evidence for unexpected parameter changes or write risks; investigate before resuming.

Per-batch Feishu summaries go to 郜远 and distinguish replacement success/failure/unknown, Creative deletion success/failure/unknown, references deferred, review waits and backlog. First cohort can notify while the full scan has pending work. Pure waiting rehoming/resource counters do not resend an unchanged outcome. Late deletion/reconciliation changes produce a new stable end-N phase. Preserve frozen body, UUID and receipts; sending/unknown notices are not blindly replayed.

## Deployment and rollback

Use deploy/install_cover.py prepare/apply/rollback from the exact GitHub release. Test locally, commit/push, fetch exact SHA, back up code/config/units/ledgers, and verify before activation. Keep the old deletion timer off. First live cohort is at most 256 naturally matching Ads; validate actual throughput/rate limits, parameter readbacks, deletion receipts and Feishu before continuing.

Stop new admissions and drain in-flight operations on rollback. Restore code/config/units while keeping current ledgers and additive shared guards so unknown fences remain effective. Never overwrite progress with an old SQLite backup. Deleted Creative IDs cannot be restored by switching old code or rebinding the old ID. Deployment and real-write claims require current server receipts; local tests alone do not establish completion.
