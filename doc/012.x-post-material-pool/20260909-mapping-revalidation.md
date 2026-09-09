# X material mapping revalidation — 2026-09-09

## Problem and change
Forty unbound pool rows retained drama_mapping_ambiguous. A read-only recheck found 36 currently valid video metadata records (14 en / 22 ja), two non-video records and two inactive records. The scheduler excluded the historical mapping code before consulting the current source.

Allow this exact code into the existing revalidation candidate scan. Do not bypass selector identity checks or clear errors on enumeration. A still-conflicting mapping remains failed. Successful prepared queue reservation clears its selected error transactionally. The existing explicit media backfill can clear errors only after source/media preflight succeeds, without creating queues or publishing Posts.

Current exact language-capacity proofs may preserve the old error on unselected candidates whose source mapping was just revalidated; missing/stale/mismatched proofs and other mapping errors remain rejected. No ROAS, language, replay, or duplicate-publication policy changes.

## Validation
232 pool/store/scheduler/backfill tests passed, including current failure retention, selected atomic error clearing, queue deduplication and fresh versus stale capacity proofs. Run selector/daily tests too before release.

## Deployment and rollback
Deploy only features/x_posts/service.py from the verified GitHub commit to a copy of the current immutable X release and the main API copy; preserve all other active files. Back up SQLite via its online backup API, both runtime copies, release target and timer states first. Pause execution triggers, drain active work, verify no uncertain in-flight writes, then restart only X sidecar and main API. Revalidate the explicitly frozen unbound material set with x_post_media_repair_backfill.py. Never replay historical runs.

Rollback code by restoring the captured main API service.py and previous /opt/x-post-automation/current target, then restarting the two services and restoring captured timer states. Do not restore the old database over new publish history. Per-material validation changes require compare-and-swap against their recorded after-state if separately rolled back.
