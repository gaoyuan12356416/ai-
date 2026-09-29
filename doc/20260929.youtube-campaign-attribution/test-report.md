# Test report

Local completed: Python compile passed for matcher, acceptance script and deployment script. Analytics unittest suite 57/57 passed. Existing analytics deployment suite 3/3 passed. git diff --check passed. New deployment fault suite 12/12 passed, including live/backup drift, restart/health failure recovery, post-replace exception recovery and database preservation. Independent QA review found no remaining blocker.

True-source snapshot replay: all revenue reconciles on the three inspected UTC dates; split/mislabeled manual rows recover to frozen campaign identities without source writes. Tenant and owner filtering are applied only after global resolution. Detailed private evidence remains outside Git under D:/codex/audits/youtube-attribution-20260929.

Production deployment and authenticated readback are pending; do not treat this document as production success until deployment-evidence.md records the exact release and completed checks.
