# X material product selection

The material picker now offers `Drama-社媒专用素材` (default) and `Dramawave`.
Browsing and text search use the selected product. A numeric material ID still
resolves across products under the existing video, drama, language and delivery
validation rules. Switching products starts at page one and preserves staged
selections; no publication or pool operation is performed by the picker.

The reported complete English drama-title search with Japanese selected returned
HTTP 503 four times on 2026-10-23 at 11:52-11:53 Beijing time. Its all-product
correlated search hit MySQL 3024 at the existing 8-second statement deadline.
Restricting products alone still timed out for the large Dramawave catalog.

Complete drama titles now resolve through `ads_drama_resource.name` (app 1479),
then fetch material rows through `(data_source,data_source_id)`, enumerating
every source kind with an index grouping. A recognized title with no matching
language returns an empty result without falling back to the expensive text
scan. Numeric IDs remain exact; other text retains product-scoped substring
search and the original 8-second deadline. Broad fuzzy searches can still be
expensive; no unlimited scan or raised timeout is introduced.

Preliminary live SQL checks: the reported title's Dramawave/Japanese material
query returned zero in 1.704 seconds; English returned 51 raw rows in 1.637
seconds. These SQL timings exclude title resolution, metadata validation and
HTTP overhead and do not constitute browser acceptance by themselves.

The implementation is based on the live shared-access release `fbfc738d28`,
preserving the same-day account-sharing changes. Only four runtime files are
installed: the source repository, HTTP gateway, picker JS and picker HTML.

## Validation and deployment

- `python -m unittest discover -s scripts -p 'test_x_video_publish_*.py'`:
  110 tests passed on Windows.
- `node tests/x_video_publish_frontend_test.cjs`: 15 shipped-script cases passed,
  including product changes, pagination reset, retained selections and failed
  versus empty reads, plus the existing shared-account publishing workflow.
- `node --check static/x-video-publish.js`; `git diff --check`.

Deploy only the exact pushed GitHub commit with
`python3 scripts/deploy_x_catalog_product.py`. The guarded installer checks all
previous file hashes against the live baseline, waits for workers and publishing
locks, backs up code/SQLite/tokens, restarts the main API and X service dependency
group, and restores the original trigger and personal-pool scheduler states.
Capture the personal scheduler state before the shared pause context stops it.
Compare publisher ledger and token fingerprints across the cutover.

Rollback from the same checked-out commit:

```sh
python3 scripts/deploy_x_catalog_product.py --rollback /absolute/backup/manifest.json
```

Rollback restores only the scoped code files. Current queues, pool state,
publication receipts, credentials and unrelated services remain current.
No test Post, new admission or historical replay is part of acceptance.
Skill context is unchanged; this repository note carries the change contract.
