# Ready counter stage separation

The monitor summed ready preparations and ready publications, then labeled the combined total as pending publication. The observed20 consisted of15 X preparations,2 X publications and3 scheduled YouTube publications.

Fix: derive separate ready_prepare and ready_publish cards from the existing owner/channel-scoped summary.channels rows. Keep missing counts unknown. Keep the established API totals.ready aggregate unchanged for compatibility. A note explains that readiness still requires schedule, channel and account conditions. Add a tenth card and bump JS/CSS cache keys.

Only pipeline-monitor.js/.css/.html are deployed to the main static directory and Nginx root. No coordinator, API, CPU/GPU worker restart; no queue, configuration, account or publication writes.

Validation: node --check static/pipeline-monitor.js; Python deployment-helper compilation; real Chromium regression with20->15/5, YouTube-only0/3, null counts, permissions,15-second refresh, capacity editing fixtures, mobile layout and deep links all passed. Fixtures use loopback only and make no production writes. Desktop screenshot inspected.

Deployment: use scripts/deploy_pipeline_monitor_static.py with exact GitHub SHA and expected baseline37395b957fa45fb4260c0a2787f07a4604d5eadd. The helper guards both live roots, backs up exact files, replaces assets before cache-busted HTML and verifies public HTTPS content. It now covers CSS as well as JS/HTML. Actual SHA, backup and readback are appended after deployment.

Rollback: invoke the same helper with --sha37395b957fa45fb4260c0a2787f07a4604d5eadd and --expected-sha equal to this deployed fix, plus --apply. Preserve all service state and ledgers.

This project-specific defect is documented here; personal skill context is unchanged.
