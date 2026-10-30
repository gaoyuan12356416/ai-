# Production verification — 2026-10-30

Code commit: `2f184bed5a814baddd5f786aa559130a77320eff`, pushed to
`codex/social-fb-pool-count-20261030` before CPU fetch and checkout.

CPU host `43.166.187.96` deployed the report adapter to
`/root/drama_material_service/features/social_analytics/facebook.py` and the
independent report release
`/mnt/data-disk/youtube-publisher-daily-report/releases/2f184bed5a81-fb-pool-owner`.
Both files have SHA256 `58ff73828eeb62a4558119226bb860bef3cdcfab16349fc1866761fd6e5324a9`.

- Linux regression: 106 tests passed; local Windows run passed with three
  Linux-specific skips. Exact pool/item/run/tenant matching, immutable history,
  owner changes, unresolved owners and unknown outcomes are covered.
- Shadow replay: 22,113 legacy and 918 current identities, zero conflicts;
  498 pool provenance rows frozen. Original identity/evidence/freeze fields
  preserved. 460 group-pool tasks move from the scheduling operator to their
  material submitter; 432 are confirmed published on UTC 2026-10-29, 27 failed,
  one unknown. No publication retry was performed.
- Full daily preview passes `require_complete()`. Every global metric exactly
  equals the original sent report: USD 3,613.68 revenue, 740 publications,
  5,829 installs, 50,618 clicks, 89,967 visits, 217 distinct payers.
- Su Siqi's Facebook row: 432 publications, USD 46.53 revenue, 2,878 installs,
  25,545 clicks, 43,949 visits, eight payers. Across all channels: 483
  publications and USD 857.90 revenue. Original Facebook operator row changes
  from 541 to 109 publications; platform total stays 611.
- Historical sent report/card/receipt hashes are unchanged. The correction is
  a separate preview, with no Feishu send or edit.
- Main API safely restarted at 11:12 Beijing, active afterwards; attribution
  collector Result=success. Both report/collector timers remain active.
  FB sidecar and publishing-worker PIDs remain unchanged. Public reporting
  page HTTP 200.
- Main API's deployed report path, using the actual user's tenant and role,
  returns a complete Facebook-only result of 432 in 12.68 seconds; all result
  rows belong to that user. This was a backend-path check, not a browser click test.

Evidence and backup: `/mnt/data-disk/social-fb-pool-owner-20261030/` contains
`deployment.json`, `canary.json`, isolated snapshots, original adapter backup,
`historical-report-sha256.json` and `verification/previews/2026-10-29/`.
Rollback point is the release recorded in `deployment.json`:
`/mnt/data-disk/youtube-publisher-daily-report/releases/eef6d41e0ceb-report-reliability`.

Exact rollback (code only; retain current ledgers and publisher databases):

```bash
python3 /mnt/data-disk/social-fb-pool-owner-20261030/source/ops/social-fb-pool-owner-20261030/deploy.py rollback
bash /root/drama_material_service/scripts/safe_restart_drama_api.sh
```
