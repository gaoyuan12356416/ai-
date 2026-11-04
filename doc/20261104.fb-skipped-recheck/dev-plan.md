# Implementation

1. Add operator-only preview/apply recovery module and CLI. Preview on online SQLite copy; no platform writes.
2. Add optional recovery deadline, consistent prepare/complete/publish late checks. Keep original IDs and audit marker.
3. Remove whole-template backlog rejection and bound unknown drama cooldown.
4. Add strict dispatcher rearm of never-executed skipped observations.
5. Test locally and Linux; GitHub commit/pull; exact overlay with live hash checks and backups; narrowly restart FB/coordinator after draining; preserve unrelated lanes.
