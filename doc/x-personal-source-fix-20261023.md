# X personal material source repair

The account-pool screenshot contains a previous source-query error and two
`drama_mapping_ambiguous` rejections. Exact rejected IDs were not available;
the operator requested continued investigation from other evidence.

Confirmed independently against the live read-only replica:

- The Japanese catalog query with limit 30 hit MySQL error 3024 at its existing
  8-second SQL deadline. The resource table contains hundreds of episode/product
  rows per content ID. Using the indexed, product-scoped deployment table for
  the language prefilter reduced this first query to approximately 2 seconds.
  Full resource identity, canonical language and delivery-time validation still
  runs before a result is exposed. Exact-ID selection remains indexed.
- The admission source queried resource metadata across all products while its
  deployment-time validation used Dramawave app 1479. Resource reads and the
  legacy manual/pool executor now consistently use app 1479. This does not add a
  product or deletion restriction on source materials. Real conflicting titles,
  series or descriptions within the product still fail closed, with a Chinese
  diagnostic. Episode rows retain their original ID order and Python identity
  comparison; SQL collation must not collapse real differences.
- Successful reloads and admission previews previously retained an older red
  error. The UI clears that stale message only after successful reads and keeps
  the input and original unknown-operation recovery controls.

The three existing account-19 pool materials (7609526, 7621074, 7690269) resolve
identically before and after the candidate change. Historical global-pool
errors on 7390985 and 7392552 were also checked: both now resolve on the old
and candidate code, so those old errors do not identify this screenshot's cause.
The recent Japanese material sample did not reproduce the two exact conflicts.
Do not claim the screenshot's rejected rows are verified without their IDs.

## Validation

109 personal-publishing Python tests, 29 material-pool selector tests and 14
shipped-script frontend cases pass. Added cases cover cross-product metadata,
genuine in-product conflicts, final language validation, executor source scope,
and failed read/preview followed by successful retry without submission.

## Deployment and rollback

Use `scripts/deploy_x_personal_source_fix.py` from the exact pushed branch.
It verifies each previous file against d35d455, backs up code/SQLite/tokens,
drains existing workers under the original deployment locks, avoids enabled
pool trigger windows, and restarts main API and X sidecar plus its X Auto service dependency. Original trigger
states and the personal pool scheduler are restored. It verifies ledger and
token hashes without restoring any historical database or sending test Posts.

Rollback from the same source checkout:

```sh
python3 scripts/deploy_x_personal_source_fix.py --rollback /absolute/backup/manifest.json
```

The rollback is file-scoped and refuses live drift. No historical pool row,
task, Post, token or short-link state is restored. Do not use the original full
personal-module installer to roll back this patch.

Skill context was left unchanged; this repository note records the repair.
Production commit, manifest and final readback will be appended after deployment.

The initial attempt at 10:46 waited for account 19's 10:45 task to publish
successfully. Its health check detected that X Auto had been stopped by the X
sidecar's systemd dependency. All 13 files were restored to their previous bytes;
all eight trigger states and the personal pool scheduler were read back restored.
The deployment helper now restores the existing three-service dependency group.
Initial attempt manifest: `/mnt/data-disk/x-post-automation/backups/20261023T024619Z-source-dab9bf01/manifest.json`
(`failed_restored`). No historical task was retried or database restored.
