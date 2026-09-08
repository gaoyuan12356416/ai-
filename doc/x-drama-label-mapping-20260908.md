# X material drama-label mapping correction

Material 5267611 resolves to Japanese drama rGeTmo317Q / 21421. All 270 source rows agree on identity, title and description; 180 include BL and 90 omit it. Treating labels as identity incorrectly rejects this material before publication.

The pool/manual/auto-template hydration and legacy daily selector now exclude descriptive labels from their canonical identity. They merge labels case-insensitively in source order, preserving the first source tag used for attribution. Required labels, content ID, language, series code, title, description, delivery time, media and deduplication gates remain intact. This does not change the source database.

## Composite deployment

Base release: c6cf7124f0b7a64000a84362729dbec0193a7533.
The live service.py has a pre-existing 43-line overlay (SHA256 524739655582e982336a21fb0e57e94098356768ff7dd66888b3defdb4c63f37). Preserve it byte-for-byte in the new release; do not deploy the base version over it.
Main API uses an older selector. deploy/x-drama-label-mapping/main-api-selector.py retains that exact variant and applies only this fix. Original main SHA256: 62c42078db115b5dc43b1da83574116d396faa289953d53dccac307f3e2b2c2e. Original release selector SHA256: cfee266a32840b65f0ba3a270795f36103cde3bb68be5254cef8f89f9a38c4d3.

Before deployment stop the three active X poll timers and require their workers to drain, save their prior states, back up affected files and SQLite with SQLite backup API. Deploy from the pushed GitHub commit, retaining the service overlay. Restart only previously active affected long-running services, then restore timers. Verify source hashes, health and exact-ID source preflight through both variants. Clear only the old unbound pool row 936 drama_mapping_ambiguous error after successful preflight and an atomic no-queue/no-active-reservation check. Retain all queue and publish-log history. Do not synthesize an immediate publish outside the configured schedule.

Rollback: stop poll timers and drain workers, restore /opt/x-post-automation/current to the saved release and main selector from backup, restart previously active affected services and restore prior timer states. Do not restore a whole SQLite backup over newer publication history. If still unbound, restore only row 936 validation evidence from the saved audit using a guarded transaction.

## Validation

python scripts/test_x_drama_label_mapping.py (4 tests, both runtime variants)
python scripts/test_x_post_material_pool_selector.py (20 tests)
python scripts/test_x_auto_post_selector.py (22 tests)
python scripts/test_x_post_drama_selector.py (20 tests)
All 66 tests passed; git diff --check passed.


## Production result (2026-09-08 14:57:58 Beijing)

Code commit aeb516c643d18674cd80ab42980b5238eb3caa76 was fetched from GitHub and deployed on 43.166.187.96 at /mnt/data-disk/x-post-automation/releases/aeb516c643d18674cd80ab42980b5238eb3caa76, referenced by /opt/x-post-automation/current. The main selector is /root/drama_material_service/features/x_posts/selector.py. Both selectors passed exact-ID live read-only source validation for 5267611, rGeTmo317Q, ja. All 66 tests passed on the server too.

Initial deployment verification used the unsupported main /health path (404), triggering a successful rollback. The corrected deployment verified sidecar /health=200 and main /api/admin/x-posts/material-pool=401 without a Cookie, proving the real route and authentication gate. Both services and all three prior active poll timers are active.

After source validation, a guarded SQLite transaction cleared only row 936's old drama_mapping_ambiguous evidence. The production store query returned status=unpublished, availability=available, queue_id=null, last_error_code empty. Queue and publish-log counts were unchanged by this transaction. This restores normal scheduled eligibility; it is not proof of publication or a bypass of final account/media checks.

Backup and audit: /mnt/data-disk/x-post-automation/backups/20260908-drama-label-mapping-aeb516c/ (manifest.json, accounts.sqlite3, accounts-retry.sqlite3, main-selector.py, release-selector.py, service.py, deployment-result.json, revalidation-audit.json).

Exact code rollback procedure on the CPU server (only after checking workers have drained):

```bash
systemctl stop x-post-schedule.timer x-post-schedule-claim.timer x-post-manual.timer
# If any of these workers is active, wait for completion before proceeding:
systemctl is-active x-post-schedule.service x-post-schedule-claim.service x-post-manual.service x-post-daily.service
cp -p /mnt/data-disk/x-post-automation/backups/20260908-drama-label-mapping-aeb516c/main-selector.py /root/drama_material_service/features/x_posts/selector.py
ln -sfn /mnt/data-disk/x-post-automation/releases/c6cf7124f0b7a64000a84362729dbec0193a7533 /opt/x-post-automation/current
systemctl restart x-post-automation.service drama-material-api.service
systemctl start x-post-schedule.timer x-post-schedule-claim.timer x-post-manual.timer
```

Keep all new ledger state; never restore the entire SQLite snapshot over newer publications.
