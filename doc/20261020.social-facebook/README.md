# Facebook income in unified social reporting

Scope: the AI backend's automatic Facebook templates (site 2049) and new manual
video publishing (site 2310). Other legacy publishing systems are not attributed
to these users. Unmatched source effects remain visible in system-level
reconciliation, without inferring ownership from campaign names or `af_channel`.

## Accounting and interfaces

Existing `/api/social-analytics/{options,report,export.csv}` accepts
`platform=facebook`; the default selection contains YouTube, X and Facebook.
Old YouTube APIs remain YouTube-only. The `social_analytics` permission and
tenant/user scope are unchanged. Facebook Page filter values are
`facebook:<page_id>`, while link identities include the site to avoid collisions.

Each effect query/cache uses one registered site. Matching is case-sensitive on
`(platform,site_id,campaign_id)`, before user filters. Every site and channel has
source/attributed/unmatched/excluded reconciliation. Only YouTube supports its
existing verified 32-character alias. One unavailable Facebook source does not
erase known values from the other, or become a fabricated zero.

`quality.platforms.facebook.sources` exposes separate 2049/2310 states,
missing dates and safe collector freshness. `platform_metadata` and
`metric_applicability` drive labels and X-only Post/Repost presentation.
Missing source data is unavailable; known partial sums are explicitly partial.
Website, CSV and scheduled cards use the same `build_report`.

### Frozen identities

- Manual 2310: task, link, batch and frozen snapshot must agree on campaign,
  tenant, submitting user, business user ID, Page, material and content.
- Automatic 2049: task/run/template IDs, the immutable template creator and
  version 1 creator must agree. The main user directory must resolve one tenant.
  Template `owner_user_id` is the original business user ID; the Page pool owner,
  later editor and person clicking Run are not the attribution owner.
- Original creator 248 maps to their confirmed local user identity (郜远).
  Unresolved records never default to 248.
- First resolved identity and its evidence hash are frozen. Incremental runs
  reuse that identity, detect changes to the original evidence, and mark
  conflicts without replacing frozen evidence. Deleted source records remain
  conflicts, not silently removed history.

The report-owned database is `/mnt/data-disk/social-analytics/attribution.sqlite3`.
Only `scripts/social_facebook_attribution.py` writes it; report requests open it
read-only. Publisher and user databases are always opened with `mode=ro` and
`query_only`. Incremental collection scans bounded records because legacy tasks
have no reliable mutation timestamp, but only changed records are written.
The collector runs every 60 seconds; snapshots older than 300 seconds or failed
collections are marked stale. Its independent process lock serializes runs.

Confirmed Facebook publications are deduplicated by Page/platform object.
Manual success requires `published`, a Graph ID and no unknown result; the
terminal reconciliation `updated_at_utc` supplies the UTC day because the current
schema has no separate publication timestamp. Automatic success also requires
the matching confirmed publish ledger and completion time. These contribute
only `published_count`, never X's Post/Repost counters.

## Validation

Private read-only snapshot:

```powershell
python scripts/social_facebook_snapshot.py --output output/private-facebook-snapshot --ssh-helper <saved-helper>
python scripts/rehearse_social_facebook.py --snapshot output/private-facebook-snapshot --output output/private-facebook-acceptance
python -m unittest discover -s tests -p "test_social*.py" -q
node scripts/test_social_analytics_ui_browser.js
```

Snapshot outputs are private and ignored, never committed. This reads no token
columns and neither posts to Facebook nor sends to Feishu.

Fixed 2026-10-19 source baseline:

| Source | Source income USD | Attributed USD | Pending USD |
|---|---:|---:|---:|
| YouTube 2284 | 910.37 | 910.37 | 0.00 |
| X 2116 | 780.42 | 780.42 | 0.00 |
| Facebook 2049 | 11,815.42 | 5,939.83 | 5,875.59 |
| Facebook 2310 | No synced rows | Unavailable | Unavailable |

Known source income is $13,506.21; attributed income is $7,630.62.
Facebook's 235 confirmed publications plus YouTube's 21 and X's 44 make 300.
No rows for 2310 does not prove a synchronized zero. Original X unmatched
visits remain in its reconciliation even though their revenue is zero.

Acceptance covers first freeze, repeated collection, evidence conflicts,
missing/ambiguous creators, new manual snapshots, read failures/staleness,
publisher-state invariance, same campaign ID across sites, global uniqueness
before filters, tenant scope, CSV/card consistency, partial source availability,
non-X metric applicability and mobile layout.

The isolated acceptance snapshot captured at 2026-10-20 09:10 UTC froze 19,465
automatic records and one new manual record with no evidence conflicts.
Repeating collection changed zero records. Its manifest SHA256 is
`b01a5c0bae5ec0f68da42cc671e290df6bfc9409bab3adf78b7dac6a38fc328a`.
API, CSV and daily preview agree row by row, and source SQLite hashes remain
unchanged. Chromium validation passed 72 desktop/mobile checks. The production
main API and daily service both run as root, matching the private 0600 ledger.

## GitHub-first deployment and rollback

Work branch: `codex/social-analytics-facebook-20261020`.
Push a tested commit, fetch it on CPU 43.166.187.96, and check out the exact SHA
in a dedicated clean source directory under `/mnt/data-disk/social-analytics`.

```bash
python3 scripts/deploy_social_facebook.py stage --sha FULL_GITHUB_SHA
python3 scripts/deploy_social_facebook.py apply --backup EXACT_RETURNED_BACKUP
```

Stage checks the previous verified social-report baseline, mounted data disk,
source commit, runtime hashes and imports. It backs up replaced files, delivery
state and source databases, and rehearses attribution against the copies.
Apply obtains the existing daily-report lock, pauses its timer, creates the
independent attribution ledger, installs report files and collector units, then
restarts only `drama-material-api.service`. It runs a no-send preview in the
actual systemd filesystem sandbox and checks the fixed baseline and freshness.
Only after verification does it enable the new collector timer and restore the
original daily timer state. FB/X publisher PIDs must remain unchanged.

Main runtime: `/root/drama_material_service/features/social_analytics`.
Daily runtime: `/opt/youtube-publisher-daily-report/current`.
The group, Beijing 11:00 schedule, delivery database and prior receipts remain
unchanged. Facebook enters the next scheduled report; no historical resend.
FB long/short links and all publishing tables are untouched.

For a failed apply, the manifest records the failed phase; do not manually resend
a daily card or restore an old database. Use the recorded rollback:

```bash
python3 scripts/deploy_social_facebook.py rollback --backup EXACT_RETURNED_BACKUP
```

Rollback verifies both current and backup file hashes, stops the new collector,
restores this release's report files/configuration and the prior daily symlink,
and restarts only the main API. It retains attribution, all publishing facts,
short links and delivery receipts. Unknown concurrent file changes are rejected.

## Production acceptance — 2026-10-20

- Deployed code commit: `2fd79fbff5c305ef3bba788fc2f06dbbfefe0eb0`, fetched from
  GitHub into `/mnt/data-disk/social-analytics/facebook-source`.
- Backup and deployment manifest:
  `/mnt/data-disk/social-analytics/backups/20261020T093130Z-fb-2fd79fbff5c3`.
- Linux: all 154 social regression tests passed, including private file modes,
  symlink rejection and changes to either daily release during the lock window.
- Production sandbox preview for October 19: attributed income $7,630.62,
  300 successful publications; Facebook income $5,939.83 and pending $5,875.59.
  Report SHA256:
  `7e914e4ac74dcace4ebea5516c0e3e0d0b59124f875dbb68d99129ee26c5fd41`.
- Both Facebook sources froze successfully: 19,465 automatic records and one
  manual record, zero conflicts. Minute timer runs at 09:32, 09:33 and 09:34 UTC
  completed with zero changes; the attribution ledger is mode 0600.
- Logged-in browser verification: all three channels, Facebook filter,
  separate source completeness, X-only counters marked inapplicable for FB,
  and successful CSV export. The October 19 manual source remains unavailable.
- Only the main API restarted. FB/X publisher PIDs and the main `app.py` hash
  are unchanged. The delivery database logical dump hash is unchanged:
  `ffcceab8a27745d5f6a3fe06d06513b3e2a4a3251dab6547e9d7ec3f37e367de`.
- Original daily timer remains active, next run October 21 at 11:00 Beijing.
  No production daily message was sent during acceptance.

Private production `deployment.json`, `verification.json`, source backups and
the no-send preview remain in the backup directory above. Rollback command:

```bash
cd /mnt/data-disk/social-analytics/facebook-source
python3 scripts/deploy_social_facebook.py rollback \
  --backup /mnt/data-disk/social-analytics/backups/20261020T093130Z-fb-2fd79fbff5c3
```

Rollback also verifies complete old/new daily directory manifests, excluding
Python caches, before switching. Added, deleted or modified files and symlinks
are rejected; publication databases and delivery receipts are never restored.
