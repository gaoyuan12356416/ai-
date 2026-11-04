# FB skipped task recovery / 2026-11-04

User authorized rechecking runs 316 and 317, requeueing eligible skipped tasks, preserving Page 867618086440072 at daily_count=0, and preventing uncertain outcomes from blocking later publications.

Recover only original fb_auto_page_unknown_block rows with no selected material, media, link, claim, attempt, receipt, or unknown outcome. Preserve run/task IDs and original schedule in selection_json.recovery. Revalidate current enabled template/version, Page ownership/language/token eligibility, source rules, all-time known publication duplication, active/unknown material reservations and 24h drama cooldown. Two runs reserve sequentially in one transaction. No existing publish task is reset.

Recovery is explicit, idempotent and has a fixed 24h execution deadline; ordinary auto late limits remain unchanged. The central dispatcher may reopen only generation-zero skipped observations with no execution history. Remove whole-template backlog rejection; retain per-slot capacity, unique reservations, Page write mutex and exact-material unknown fences. Unknown drama cooldown expires normally. Reconciliation remains bounded asynchronous GET-only.
