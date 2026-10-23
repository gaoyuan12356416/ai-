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

Deployment and live acceptance must be recorded below before claiming completion.
Skill context is unchanged; this repository note carries the behavior contract.
