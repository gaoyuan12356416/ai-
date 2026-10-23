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
