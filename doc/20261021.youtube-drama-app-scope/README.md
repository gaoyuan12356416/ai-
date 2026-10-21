# Scope YouTube drama metadata to the material product

The YouTube material selector already assigns the target product. The drama
resolver previously read all products sharing a content ID and language, so
product-specific synopsis or cover differences could block a valid material.

Resolve text and covers by `(app_id, content_id, normalized language)`. Query
each validated numeric app ID separately and check the same identity on returned
rows. Missing/invalid app IDs remain unresolved; never guess another product.
Conflicting metadata within the requested product still blocks as before.

Preserve the deployed 20-content-ID batches, global 1000-unique-row limit,
read-only replica/FIFO gate, timeout, exact content-ID comparison, cover guards,
material selection, immutable task copy and publication ledger.

## Validation

Run `python scripts/test_youtube_drama_association.py`,
`python scripts/test_youtube_auto_service.py`,
`python scripts/test_youtube_reference_workflow.py`, and
`python scripts/test_youtube_drama_app_scope_deploy.py` on Windows and CPU Python.
Tests cover different products with the same content/language, wrong-product
fallback rejection, product-specific covers, invalid product IDs, same-product
ambiguity, query bounds and frozen workflow/link identity.

## Deployment and rollback

Push the exact commit to GitHub, fetch that commit into a clean CPU release
checkout, run the tests and `python3 scripts/deploy_youtube_drama_app_scope.py
--check`, then run the installer without `--check`.

Only `features/youtube_auto_publish/drama.py` is installed into
`/root/drama_material_service`. The installer checks the previous live SHA,
backs up the file and online SQLite using the existing installer, then restarts
only the API. It sends SIGTERM to the auto-publisher main process to let current
work finish before Restart=always loads the new module. Confirm a new active
worker PID; a drain request alone is not acceptance. Legacy publisher and unified
writer state must remain unchanged.

For the exact backup path returned by deployment, run:

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/<commit>/scripts/deploy_youtube_drama_app_scope.py --rollback <backup>
```

Rollback rejects later file drift and restores code only. Retain the current
database, assets, short links and all publication facts. Validate the target
material through fresh read-only source lookup and authenticated materials GET,
then verify frontend macro preview without submitting a publication.

The local source baseline predates the deployed batching fix; this branch also
retains the exact resolver and associated regression tests from `6acd1b4` before
applying product isolation. The production change is measured against that
verified live file, not a wholesale checkout replacement.
