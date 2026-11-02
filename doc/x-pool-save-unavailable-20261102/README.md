# Material-pool settings accept healthy subsets

The material-pool settings page could display 95 selected accounts, but both the
submitted scope and the complete eligible-account catalog were validated against
an obsolete limit of 50. Even a small selection failed when the eligible catalog
contained more than 50 entries. Settings and frozen-slot execution now share a
200-account limit, within existing SQLite query bind budgets.

Material-pool saves use the current safe account projection and persisted account
holds. Known unavailable accounts are removed before eligibility validation and
future plan generation, with `item.skipped_account_ids` returned to the browser.
This does not verify accounts against X, refresh tokens, clear holds or modify
historical queues. If all selected accounts are unavailable, the settings save
with an empty scope and automatic publishing disabled. Recovered accounts must
be selected again. Unknown IDs, malformed input, permissions, version conflicts,
slot freeze windows and drama owner protections retain their existing behavior.

The material page lets known unavailable selections reach this server decision
and displays the skipped count. A status change after saving is still caught by
the existing schedule preflight, reservation and final publishing checks.

Validation includes real Sidecar save-handler/store integration with 95 healthy
and four unavailable accounts, ledger holds, all-unavailable input, unknown and
malformed IDs, permission/version conflicts, frozen plan scope and zero Posts.
The actual browser save function is executed for mixed, all-unavailable and
missing-account cases. Existing account tests include three obsolete ownership
assertions predating the deployed 2026-10-29 authorization-owner transfer policy;
these are unrelated to this change and are not rewritten here.

Deploy the exact pushed commit as an overlay on the verified running release.
Update the main API's `service.py` and shared account limit plus both static page
copies. Drain accepted publications before restarting the Sidecar, restore its
dependent Auto service and original triggers, and retain current database/token
state on rollback. The deployment manifest records the previous release, changed
external files and original service/trigger states.
