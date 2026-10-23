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

## Production receipt

- GitHub release: `9695b734675dd9a421800b8e70233f138c9d8160`.
- CPU host: `43.166.187.96`. Main API: `/root/drama_material_service`;
  X runtime: `/opt/x-post-automation/current`; public static: `/usr/share/nginx/html`.
- Pulled source: `/mnt/data-disk/x-personal-publishing/catalog-product-9695b73`.
- Backup: `/mnt/data-disk/x-post-automation/backups/20261023T064254Z-source-9695b734/manifest.json`.
- Installer status `deployed`. All 10 installed file hashes and both public
  static-asset hashes matched. Ledger and token fingerprints were unchanged
  during cutover; original triggers and the active personal pool scheduler
  were restored. Main API, X sidecar and its Auto dependency are active.
- Linux regression: the same 110 backend tests passed before deployment.
- Candidate full-source reads (including metadata validation) returned 10
  social videos in 8.158 seconds; the reported full title returned no Japanese
  videos in 4.097 seconds and 48 English videos in 8.771 seconds. Exact
  material ID 7783665 matched despite selecting the social product, 6.703
  seconds. These are timestamped samples of a changing source library.
- Browser verification loaded the social picker, switched to Dramawave and
  rendered 48 real title-search results while retaining one staged selection.
  The original full-title plus Japanese filter returned the normal empty state
  without a red error. Nginx confirmed HTTP 200 for these same browser requests.

Exact code rollback on the CPU server:

```sh
cd /mnt/data-disk/x-personal-publishing/catalog-product-9695b73
python3 scripts/deploy_x_catalog_product.py --rollback /mnt/data-disk/x-post-automation/backups/20261023T064254Z-source-9695b734/manifest.json
```
