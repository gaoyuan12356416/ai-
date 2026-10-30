# Production verification — 2026-10-30

Runtime commit: `d1369982325b4ef98aa67bb3c7934100d65b545b`, pushed to
`codex/social-fb-pool-breakdown-20261030` and fetched from GitHub on CPU before
deployment. Main API restarted successfully at 16:01:33 Asia/Shanghai.

Host: `43.166.187.96`. Main code:
`/root/drama_material_service/features/social_analytics/{facebook,report}.py`.
Independent report release:
`/mnt/data-disk/youtube-publisher-daily-report/releases/d1369982325b-fb-pool-breakdown`.

- Windows regression: 122 run, 119 passed and three platform skips. CPU Linux:
  all 122 passed. `py_compile` and `git diff --check` passed.
- Isolated real-ledger replay: 23,037 identities, 504 pool identities
  reclassified. Only the returned `link_type` and label changed; every other
  returned field and the ledger file hash remained equal. Counts and synthetic
  metric totals conserved. This replay did not query effect SQL or write ledgers.
- Real authenticated browser query for UTC 2026-10-29 now shows four groups:

| Type | Published | Clicks | Views | Installs | Payers | Revenue USD | Links |
|---|---:|---:|---:|---:|---:|---:|---:|
| Group pool | 432 | 25,545 | 43,949 | 2,878 | 8 | 46.53 | 432 |
| Automatic template | 109 | 13,131 | 25,812 | 1,076 | 27 | 690.02 | 0 |
| Manual | 61 | 3,294 | 4,449 | 519 | 9 | 49.62 | 9 |
| Page pool | 9 | 34 | 109 | 7 | 0 | 0.00 | 9 |

Every metric total equals the pre-deployment browser result: 611 publications,
42,004 clicks, 74,319 views, 4,480 installs, 44 payers, USD 786.17 revenue,
USD 0 refunds and 450 created links. All four groups show complete source data.
Browser checkbox filtering to both pools returns exactly two groups, 441
publications, 25,579 clicks and USD 46.53. Restored the original date, channel
and all-link-type grouping after verification.

Main API is active, public reporting page HTTP 200, no error-priority startup
logs. FB sidecar PID 1339255 and video worker PID 1339338 did not change;
attribution and daily-report timers remain active. No publishing operation or
message send was invoked. Skill maintenance reference updated.

Deployment/backup manifest:
`/mnt/data-disk/social-fb-pool-breakdown-20261030/deployment.json`.
Prior report release:
`/mnt/data-disk/youtube-publisher-daily-report/releases/2f184bed5a81-fb-pool-owner`.
Code-only rollback is documented in [README.md](README.md); retain every current
publisher/report database and all historical messages.

Verified runtime SHA256:

```
facebook.py (both): 1e5757e3b486f397e6b850330e12dbbef692525be630697d49e50f558deb4289
report.py (main): 3a7ca19f3c631f7095bb1eea60d495f833caf0adafb91e8c0a54daa8b552280f
report.py (daily): 5cee176f356d6002a086f050cde0ee9ecdaf7542a9c7c87b66f244ce9d2a4a05
```
