# Implementation

1. Add a pure campaign index/resolver in features/youtube_analytics/report.py; preserve public DTO compatibility and use frozen display dimensions.
2. Update the read-only acceptance script to independently reconcile source/matched/unmatched money via ID resolution.
3. Extend regression tests through the QA workstream; compile with Python, run unittest discovery and diff checks.
4. Deploy a single runtime Python file from an exact pushed GitHub commit, with hash-guarded backup and rollback. Restart only drama-material-api.service; check publisher worker PIDs.
5. Read authenticated report/CSV, warm default seven-day and incident-day caches, archive local evidence without credentials.
