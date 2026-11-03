# Ready counter stage separation

The monitor summed ready preparations and ready publications, then labeled the combined total as pending publication. The observed 20 consisted of 15 X preparations, 2 X publications and 3 scheduled YouTube publications.

Fix: derive separate ready_prepare and ready_publish cards from the existing owner/channel-scoped summary.channels rows. Keep missing counts unknown. Keep the established API totals.ready aggregate unchanged for compatibility. A note explains that readiness still requires schedule, channel and account conditions. Add a tenth card and bump JS/CSS cache keys.

Only pipeline-monitor.js/.css/.html are deployed to the main static directory and Nginx root. No coordinator, API, CPU/GPU worker restart; no queue, configuration, account or publication writes.

Validation: node --check static/pipeline-monitor.js; Python deployment-helper compilation; real Chromium regression with20->15/5, YouTube-only0/3, null counts, permissions,15-second refresh, capacity editing fixtures, mobile layout and deep links all passed. Fixtures use loopback only and make no production writes. Desktop screenshot inspected.

Deployment: use scripts/deploy_pipeline_monitor_static.py with exact GitHub SHA and expected baseline37395b957fa45fb4260c0a2787f07a4604d5eadd. The helper guards both live roots, backs up exact files, replaces assets before cache-busted HTML and verifies public HTTPS content. It now covers CSS as well as JS/HTML. Actual SHA, backup and readback are appended after deployment.

Rollback: invoke the same helper with --sha 37395b957fa45fb4260c0a2787f07a4604d5eadd and --expected-sha equal to this deployed fix, plus --apply. Preserve all service state and ledgers.

This project-specific defect is documented here; personal skill context is unchanged.

## Deployment and acceptance

- Deployed commit: `07692ce3d11ed34c44ae966d6061c4fa9f02b5c6`, pushed to GitHub before deployment.
- Host: CPU `43.166.187.96`; static directories `/root/drama_material_service/static` and `/usr/share/nginx/html`.
- Applied at 2026-11-03 18:40:40 Beijing. Both directories matched the expected baseline before replacement. Public HTTPS bytes matched GitHub for JS, CSS and HTML after replacement.
- Backup and machine-readable report: `/mnt/data-disk/pipeline-dispatch/monitor-static-20261103T104040Z/deploy.json`.
- Coordinator remained active with PID `2489229`, started at 18:14:37 Beijing. Deployment performed no service restart or task/configuration/ledger writes.
- Read-only ledger check at 18:41:08 Beijing: ready preparation 15 (X); ready publication 4 (X 1, YouTube 3). Counts can change as the existing workers run. The regression snapshot of 20 still correctly renders 15 preparations and 5 publications.
- Browser regression includes channel-filtered counts, missing-value preservation, mobile layout and existing monitor interactions. Desktop screenshot visually checked. No test platform posts were created.

Rollback command on the CPU host (exact version guard; no restart):

```sh
git -C /mnt/data-disk/pipeline-dispatch/github show 07692ce3d11ed34c44ae966d6061c4fa9f02b5c6:scripts/deploy_pipeline_monitor_static.py | /usr/bin/python3.9 - --sha 37395b957fa45fb4260c0a2787f07a4604d5eadd --expected-sha 07692ce3d11ed34c44ae966d6061c4fa9f02b5c6 --apply
```
