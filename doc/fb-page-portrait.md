# Managed Facebook Page income portraits

Page 管理 gains creator filtering and a seven-complete-report-day income portrait per configured Page. The reviewed group import uses the current group's languages and trusted creator identity through the existing FB configuration API. It never enables pools, creates publishing tasks, changes Page authorization or changes X authorization.

## Accounting

- Paid: Dramawave/platform 0, `ads_custom_source_insight.af_revenue0`, report `dt`, USD (same D0 metric and currency as current Dramawave material reports).
- Social: the verified AI-backend social warehouse, native Facebook 2049/2310, confirmed publications only. Current X audience/theme taxonomy is imported from the active shared backend. SocialKit and old-platform campaigns are outside that existing X-compatible attribution scope.
- The latest seven complete UTC report dates are used. No historical fallback. A social-only Page is eligible. Current language configuration does not erase historical in-window activity in another language.
- Exact content ID + normalized language classification; product 1479 only. Unknown or ambiguous labels stay unmarked. Revenue is divided across distinct broad themes using X's cent-conserving largest-remainder allocation. Audience shares use Page total; theme shares use their audience parent. This is an income preference proxy, not demographic user counts.
- Signed corrections remain in financial totals; an affected Page does not display misleading positive audience proportions.

## Performance and permissions

Public GET `/api/fb-page-portraits?ids=...` accepts at most 20 unique IDs. Every request checks the existing Cookie and navigation/module gates. Active administrators have no source-owner filter: their current active admin membership and tenant's configured Page IDs are re-read from indexed local SQLite, avoiding a second full source list. Ordinary users retain the existing fresh Page-list source-owner gate. There is no authorization cache. Invalid or mixed unauthorized scope fails closed. No credentials or user-supplied actors are accepted. Local immutable SQLite indexes serve summaries, trees, and 50-drama pages. Portrait data reads never query source SQL. Only currently displayed rows load summaries; trees and drama pages load on demand with abort/generation protection. Creator filtering narrows the already authorized list using the immutable creator ID.

The collector pins the verified social warehouse and reads seven bounded paid date aggregations through the existing FIFO-gated reader (63350). It writes only `ads_ai.fb_page_revenue_profile` on 63353, verifies payload hashes and sums on the replica, then atomically publishes the local serving version. Any incomplete source aborts the refresh and preserves the last successful snapshot. An independent timer runs hourly at :25 plus jitter. There is no dependency on or restart of the publisher.

## Deployment and rollback

Run the unit tests with `FB_PORTRAIT_SHARED_ROOT` pointing to a current backend containing `features/x_tag_revenue`; those shared modules are dependencies, not replaced by this release. Check JS syntax, compile the new Python package, and run `git diff --check`.

Push the commit to GitHub, fetch its exact hash, then run `ops/fb-page-portrait/deploy.py --source CHECKOUT --commit HASH --deployment /mnt/data-disk/DEPLOYMENT`. The installer fences the two current public baseline assets, backs them up, installs only the independent portrait process and Nginx routes, and copies its four static assets to Nginx and the main fallback. It does not replace the active FB release or restart FB/main services. A stale unrelated FB release's static bundle must not overwrite these public assets during later deployments.

Review and hash the current group 62 manifest, run `features.fb_page_portrait.import_group` dry-run, then `--apply --expected-sha256 HASH`. A SQLite online backup and per-row snapshots are retained. Execute the initial refresh with `--create-table` once; enable `fb-page-portrait-refresh.timer` only after successful database and serving verification.

Rollback code: `python3 CHECKOUT/ops/fb-page-portrait/deploy.py --deployment /mnt/data-disk/DEPLOYMENT --rollback`. This removes only this feature's public routes/services/assets and restores the backed-up assets. It preserves imported Page configurations, financial snapshots, queues, publishing receipts and existing Page languages. Reversing an import is a separate, version-fenced operation through the existing delete-config API; never restore the full live publication database from its backup.
