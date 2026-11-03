# Deployment

Pending exact commit and evidence. GitHub-first; snapshot DBs/code/configs; compare expected live hashes; apply canonical bridge and main runtime modules. Use existing guarded coordinator installer to drain only control HTTP requests, preserve leases/configuration and all worker PIDs. New bridge has its own unit. API reload only after checking in-process work. Code-only rollback; never restore SQLite snapshots over publication receipts.
