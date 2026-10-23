# Facebook manual / Excel publishing policy, 2026-10-23

The operator removed release/deploy-time eligibility, Page/material language matching,
required drama name/description, pre-existing Page configuration, manual daily quota,
200-pair/row and 2 MiB Excel caps, and required/length/macro restrictions for manual copy.
The earlier manual blacklist waiver stays enabled. Pool and old automatic selection,
quota, and template validation retain their strict paths.

Each manual caption and title independently resolves explicit nonblank input, then the
Page default, then empty. Page settings now have an optional default title. Defaults are
frozen at submission; later edits never rewrite accepted tasks. Missing name/description
macros become empty; unknown macros remain literal. Missing drama attribution uses none.
An accessible Page without settings uses a virtual version-zero config without creating
an actual config. Preview/submit version consistency, authorization and tenant mapping,
video validity, schedule checks, active/unknown-outcome fences and repeat confirmation
remain in place. Facebook can still reject content under its own API rules.

There is no 200-task business cap. Infrastructure is bounded: HTTP JSON request/response
64 MiB, Excel expansion 256 MiB and 4 million scanned cells. Queries remain chunked.
These transport/parser protections are not a claim of unlimited upload capacity.

## Validation

Isolated regressions cover 205 manual tasks and 205 Excel rows with daily_limit=1,
source SQL batches 100/100/5, unreleased/missing metadata, cross-language and unconfigured
Pages, independent caption/title defaults, frozen history, long text, >2 MiB XLSX and
existing duplicate/idempotency/unknown fences. Browser fixture checks Page title editing,
cross-language pairing, virtual Page config, fallback preview and independent field edits.
No test publishes to Facebook. Production verification uses preview routes only.

## Delivery and rollback

Branch: codex/fb-manual-gates-20261023. Baseline: 6a4e1f9 (runtime previous release 7b64d83).
Run scripts/deploy_fb_manual_gates.py first dry, then --apply from the exact pushed GitHub
checkout. It checks hashes, a safe pool schedule window, drains producers, creates an
online DB backup, rehearses migration on a copy and verifies all old column hashes,
adds only fb_video_page.title, installs allowlisted files and restores service states.
Only the FB copy of fb_auto_posts/service.py is changed; the divergent main copy stays
untouched. Nginx changes only the FB API location body budget. Main API and FB sidecar
restart for their loaded Python changes; publisher schedules retain their prior states.

Rollback command: python3 scripts/deploy_fb_manual_gates.py --rollback <manifest.json>.
It refuses pending or unknown manual tasks before and after draining. Reconcile those
before rollback. Keep the live DB, additive title column, frozen snapshots and short links;
never restore the old DB over accepted publication facts. Backup and verified release
identifiers are appended after deployment.

## Verified deployment

Production code: 87e5b1c5ba8dcd28bb1ce5cf6ad823b89548a27c, deployed 2026-10-23
02:38 UTC on 43.166.187.96. GitHub checkout:
`/mnt/data-disk/fb-video-publish/releases/87e5b1c`.
Runtime: `/opt/fb-auto-post/current`; main client `/root/drama_material_service`;
public static `/usr/share/nginx/html`. All 33 installed hashes matched.
Main API and FB sidecar restarted successfully, Nginx validated/reloaded; all saved
service/timer active/enabled states matched after deployment.

Backup manifest:
`/mnt/data-disk/fb-video-publish/backups/manual-gates-20261023T023806Z-87e5b1c5/manifest.json`.
Copy rehearsal and live additive migration preserved 142118 existing rows across 29
business tables. Readback preserved 110 existing task snapshots and 110 link rows;
SQLite quick_check returned ok. No historical retry, test Post or live config edit.

Production read-only checks: all six supplied formerly blacklisted materials resolve;
manual preflight accepts six pairs on an authorized Page without settings; blank
caption/title stay empty when that Page has no defaults. A 4199610-byte XLSX returned
6 valid / 0 skipped. The publishing picker includes the virtual configuration. All
verification operation IDs have zero created batches. Evidence files under
`/mnt/data-disk/fb-video-publish/evidence/`: `manual-gates-catalog-20261023.json`,
`manual-gates-deployed-20261023.json`, `manual-gates-readback-20261023.json`.
Verifier correction 268a69f supplies required null per-material schedules and changes
no production runtime. Five public assets returned 200 with exact hashes; unauthenticated
Page API returned 401 and private identity broker 404.

Local validation passed: core 49, Excel 26, manual-gates 49, manual-blacklist 45,
Page-pool 44, pool-extension 19, pool-ID 12, source/link 37, gateway 12, broker 18 tests
(some fixtures are inherited, so these counts are not unique cases). Browser fixture:
10 checks passed, no JavaScript errors in the mocked authenticated flow. JS syntax and
git diff whitespace checks passed. Skill context updated for the scoped policy.

Exact rollback, only after pending/unknown manual work is reconciled:

```bash
cd /mnt/data-disk/fb-video-publish/releases/87e5b1c
python3 scripts/deploy_fb_manual_gates.py --rollback /mnt/data-disk/fb-video-publish/backups/manual-gates-20261023T023806Z-87e5b1c5/manifest.json
```
