# YouTube long-link attribution

New reviewed-cover tasks using `{url}` retain landing `/ads/101/2284/view` and freeze eight fields:

| Field | Source |
|---|---|
| c | `yingliang_post_CLV_VL_youtube_{channel_id}*{ledger_created_unix_seconds}none{language}*{drama_name}*{tag_or_none}*{publish_ledger_id}` |
| af_adset | Trimmed YouTube channel name |
| af_adset_id | Real YouTube channel ID |
| af_ad | `{source_material_name}_contentid[{content_id}]` |
| af_ad_id | Source material ID |
| af_channel | Creator's tenant-scoped email -> admin_user_group.email -> unique positive sub_user_id |
| af_c_id | This YouTube preparation ID, including materials originating from a synthesis job |
| af_dp | Drama content ID |

URL values are encoded once with UTF-8 percent encoding, retaining `*` campaign separators. Campaign components cannot contain `*`. Missing metadata or email mapping and multiple distinct IDs fail closed; repeated group rows with the same ID are accepted. Mapping is a bounded SQL-gated read on replica 63350, using trim plus case-insensitive email equality, explicit binary collation and no admin_users join. Never substitute the Feishu user ID, display name, or account ID.

The preparation reserves an immutable numeric short URL and attribution context atomically. During cover review its redirect is not published yet. After approval the shared publication ledger assigns the real publication ID; it remains `attribution_pending` and is excluded from every reviewed claim until its wrapper is published and read back. The preparation update and upload eligibility change commit together. Response loss or filesystem failure retries the same ledger/short URL; it cannot create a duplicate upload. The wrapper becomes usable before the video upload starts.

`youtube_link_attribution` is an additive SQLite table. Historical preparation bodies, ledger rows and wrappers are not backfilled or rewritten; tasks created before this release continue their frozen legacy contract. Templates without `{url}` retain their prior path. Original synthesis source identity remains metadata; each new YouTube task gets its own link attribution.

## Validation

`python scripts/test_youtube_attribution.py -q` exercises 8 scenarios with real isolated SQLite and immutable files, without YouTube, email or Feishu writes. `python -m unittest discover -s scripts -p 'test_youtube_*.py' -q` covers the preparation, image, channel, schedule, upload, unknown-outcome and comment flows. Also run the synthesis/store and legacy YouTube suites because the shared store has additive opt-in changes.

## Deployment and rollback

Use `scripts/deploy_youtube_attribution.py --commit SHA --check`, then the same command without `--check`, from the exact GitHub-fetched release. It verifies current file hashes, material SQL, mount UUID and idle queues, backs up files and SQLite, then changes five Python files and restarts the API plus two dependent workers. The unified writer and Hong Kong executor stay running. Existing platform posts and short wrappers are untouched.

Rollback: `python3 RELEASE/scripts/deploy_youtube_attribution.py --rollback BACKUP`. It verifies no later code drift or unfinished new attribution preparations, restores only code, restarts the same services, and retains database/history/assets. Never restore the SQLite backup over subsequent publishing history.
