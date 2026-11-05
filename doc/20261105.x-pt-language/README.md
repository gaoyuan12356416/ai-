# Portuguese material/account matching

Manual X publishing compared the catalog's `pt` with the account's `pt-br`
as different languages, disabling the pair and rejecting backend preflight.
The language filter also omitted these materials.

The comparison now treats exactly `pt` and `pt-br` as aliases, including case
and underscore normalization. Stored account settings, source languages,
frozen task tags and receipts are retained. Other regional tags (including
`pt-pt`) and other languages remain distinct. The manual page uses the same
rule for pairing, validation, filters and labels, including restored drafts.
Both SQL prefilter and final source filtering accept the two codes.

Validation: `python -m unittest scripts.test_x_pt_language
scripts.test_x_account_language_routing -q` (11 passing),
`node tests/x_video_publish_frontend_test.cjs` (15 passing), JS syntax,
affected Python compilation and `git diff --check`. All fixtures are isolated;
no real batch, publication or account setting is created by tests.

Deploy the pushed commit using `scripts/deploy_x_pt_language.py`:
`--phase prepare`, then `--phase drain --folder <backup>`, then
`--phase switch --folder <backup>`. The installer checks affected live hashes,
preserves current composite overlays, pauses only X admissions/triggers,
lets current X jobs finish, and briefly drains API connections before switching
the five affected files. Main API/X Sidecar are restarted; the existing Auto
dependency and original trigger/channel states are restored. Other channel
schedulers and workers are not stopped. Main/public/runtime static copies
are synchronized with a new browser cache version.

Code-only rollback: run `--phase rollback --folder <backup>` then
`--phase switch --folder <backup>` using this same installer. If a drain is
abandoned, use `--phase abort --folder <backup>` to restore admissions.
Rollback never restores databases, tokens or publication facts. Backup folder
`manifest.json` records exact source commit, paths, hashes and lifecycle state.

## Production receipt (2026-11-05, Beijing)

- Deployed commit: `85b4a083eecbc9eff4f131fc0b5a86dae4fd8102`.
- Host: `43.166.187.96`; source: `/mnt/data-disk/x-pt-language-20261105/source`.
- Updated main root `/root/drama_material_service`, active X runtime
  `/opt/x-post-automation/current`, and public `/usr/share/nginx/html`.
- Backup: `/mnt/data-disk/x-post-automation/backups/20261105T072232Z-pt-85b4a083`.
- No running X jobs or active X callers at cutover. Main API, X Sidecar and
  dependent Auto service healthy; prior trigger and X admission states restored.
- Exact live Portuguese material query with the `pt-br` filter returned its
  original `pt` source tag; comparison with the reported account passed.
- Public HTML/JS hashes match deployed source. Token hashes unchanged.
- Eleven Python checks also passed on production Linux. No test batch or Post.

Rollback, preserving all current publication data:

```sh
cd /mnt/data-disk/x-pt-language-20261105/source
python3 scripts/deploy_x_pt_language.py --phase rollback --folder /mnt/data-disk/x-post-automation/backups/20261105T072232Z-pt-85b4a083
python3 scripts/deploy_x_pt_language.py --phase switch --folder /mnt/data-disk/x-post-automation/backups/20261105T072232Z-pt-85b4a083
```

If switch reports `waiting`, let the affected work finish before repeating
the switch command; do not terminate tasks or clear leases.
