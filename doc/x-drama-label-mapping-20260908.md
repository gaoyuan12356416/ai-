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
