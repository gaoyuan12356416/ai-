# X operator material source gates

Exact IDs added to the X material pool or submitted in operator manual batches
may belong to any product, including blank product metadata, and may have a
soft-deleted source row. This applies to the existing supported video and image
types. The source row and usable media URL must still exist.

Keep source identity, duration, drama mapping/delivery time, full media download
and probe, account authorization, language, Premium routing, automatic dedupe,
and unknown-outcome fences. Do not update ads_custom_source or deletion flags.
Automatic-template source selection and the drama pool keep their prior policy.

Historical unbound material_product_mismatch and deleted-image rejections are
revalidatable. Enumeration preserves their errors; only successful full preflight
and audited refresh or atomic queue reservation can clear them. Bound queues
and prior publish history remain occupied and immutable.

## Deployment and rollback

Preserve the live composite including label-mapping normalization and account
failure isolation. Fetch the pushed commit; overlay only selector.py and
service.py on a copy of the current X release. Install those exact files in both
the main API and X runtime. Stop active X timers, drain publishers, take the shared
runner lock, back up code and SQLite, and record token hashes/modes. Restart the
main API and dependent X sidecars, verify health and unchanged ledgers, then restore
the previous timer states. Do not create a Post or queue for deployment validation.

Revalidate only the explicitly identified unbound rows with the existing media
backfill command after backup. Never clear product errors with bulk SQL.

Rollback under the same drain/lock: restore the recorded previous release symlink
and main API selector.py/service.py backups, restart affected services, verify
health, and restore timers. Preserve current SQLite, tokens and publishing facts.

## Validation

Offline pool/manual product and soft-delete combinations, automatic-template
policy boundary, duration/mapping failures, historical-error revalidation, FIFO
and global dedupe, media backfill, manual run and scheduler suites.
