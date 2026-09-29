# Test report

Local completed: Python compile passed for matcher, acceptance script and deployment script. Analytics unittest suite 57/57 passed. Existing analytics deployment suite 3/3 passed. git diff --check passed. New deployment fault suite 12/12 passed, including live/backup drift, restart/health failure recovery, post-replace exception recovery and database preservation. Independent QA review found no remaining blocker.

True-source snapshot replay: all revenue reconciles on the three inspected UTC dates; split/mislabeled manual rows recover to frozen campaign identities without source writes. Tenant and owner filtering are applied only after global resolution. Detailed private evidence remains outside Git under D:/codex/audits/youtube-attribution-20260929.

Production deployment completed. Linux also passed 69/69; authenticated loopback and public HTTPS readback passed, caches refreshed, worker PIDs and frozen links preserved. See deployment-evidence.md for exact release and rollback.
