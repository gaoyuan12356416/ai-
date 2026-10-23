# X material title search across translations

The complete English title of a translated drama matched only its English
`content_id`. Selecting all languages therefore still returned only the English
video. The reported Japanese, Korean and Traditional Chinese videos were already
in `ads_custom_source`, with valid duration and drama mappings. All three exact
resource IDs returned from the live source HTTP service before this change; the
Korean ID also rendered in the public browser. The reported earlier ID-search
failure was not reproduced and is not attributed to this title-search defect.

Complete titles now retain their direct content matches and expand by nonempty,
nonzero `series_code` through `ads_drama_info.scoo`. Both title and translated
content lookup remain scoped to app 1479, independently bounded, and use existing
indexes. Product, language, cursor, media and complete per-content validation
remain in force. Exact-ID queries retain the existing single-query path and
cross-product behavior. No schema, timeout, source cache or publication policy
changes are involved.

The regression covers English-title results across four languages, explicit
language filtering, unrelated content exclusion, product and cursor filtering,
mapping overflow, and existing exact-ID behavior. Local Windows validation:
114 backend tests passed and `git diff --check` passed.

Deploy only `features/x_video_publish/sources.py` in the main and X runtimes with
`python3 scripts/deploy_x_title_language_search.py`. The installer checks the
preceding 4317b66 file hashes, serializes deployment, drains existing publishers,
backs up code/SQLite/token files, preserves ledger and token fingerprints, and
restores the original trigger and scheduler states. Code rollback is:

```sh
python3 scripts/deploy_x_title_language_search.py --rollback /absolute/backup/manifest.json
```

Skill context is unchanged; this repository note carries the behavior contract.

## Production acceptance

- Released GitHub commit: `6958ce850b4210adfc0bf8231336ffc246a101d4`.
- CPU host: `43.166.187.96`; checked-out source:
  `/mnt/data-disk/x-personal-publishing/title-language-6958ce8`.
- Changed only the source module in `/root/drama_material_service` and
  `/opt/x-post-automation/current`; both installed hashes match the GitHub source.
- Backup and deployment receipt:
  `/mnt/data-disk/x-post-automation/backups/20261023T104858Z-source-6958ce85/manifest.json`.
  Status is `deployed`; `ledger_preserved`, `tokens_preserved` and
  `triggers_restored` are all true. The deployment waited for the existing Auto
  task instead of stopping its upload/preparation. The SSH observer timed out
  during this wait; the original deployment process completed on the server and
  was reconciled from its durable manifest, without issuing a duplicate deploy.
- Main API, X sidecar, X Auto service and personal-pool scheduler are active.
- Windows and Linux both passed 114 personal-publishing backend tests.
- Public-browser English-title search with all languages rendered 33 real
  materials, including the three reported IDs. Japanese filtering rendered five
  materials, including resource 7784520. No material was selected or published.
- After restart, production HTTP exact-ID lookups returned 7784748 (zh-tw),
  7784751 (ko), and 7784520 (ja), taking 2.534, 2.587 and 2.781 seconds respectively.
  The complete candidate title query took 24.561 seconds and the Japanese-filtered
  query 9.565 seconds, including gate/connection/mapping overhead; these are
  samples, not latency guarantees.
- Safe readbacks and screenshots:
  `D:/codex/artifacts/x-title-language-search-20261023/`.

Exact code-only rollback on the CPU server, retaining current publisher state:

```sh
cd /mnt/data-disk/x-personal-publishing/title-language-6958ce8
python3 scripts/deploy_x_title_language_search.py --rollback /mnt/data-disk/x-post-automation/backups/20261023T104858Z-source-6958ce85/manifest.json
```
