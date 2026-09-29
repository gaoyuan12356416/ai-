# Requirement: campaign ID attribution refresh

Recompute AI-backend YouTube analytics from the statistics source and immutable backend publication/manual-link records. A unique campaign ID is authoritative even when upstream campaign names or channel fields are missing or mixed. Apply to existing and future date queries, options and CSV. Preserve source metric totals, user/tenant authorization, stored links, channel ownership and publication state.

Support the source varchar(32) representation of longer frozen IDs, only when globally unique. Any competing full ID, owner, tenant, drama, channel, language, link type or exclusion state makes that lookup unresolved. Filters and names must not break ties. Full long IDs remain independently resolvable. No source SQL writes or schema changes. This release targets the AI analytics endpoint, not historical Feishu deliveries or the separate scheduled daily-report collector.

Acceptance: split name rows for one ID combine without duplicate money/link counts; synthetic collisions fail closed; actual source totals reconcile; authenticated API and CSV show corrected personal metrics; restart clears the five-minute process cache and warm the default seven-day report.
