# FB Page captions, 2026-11-13

Approved scope: active Page-management records in tenant `149bb4a14b57975f`
created by `892fd2e8` (郜远): 236 Pages. The approved 23-language bank is in
`approved-20261113.json`. Empty language uses English copy; language, title,
creator and authorization remain unchanged. Updates use the authenticated,
version-checked Page API and its normal version history.

New automatic and UI hit-material tasks select valid Page-management copy
first, then the automatic template fallback. Single-brace Page macros are
normalized to automatic macros. Missing required metadata, unknown macros,
repeated URLs, oversized expansion and unresolved Page/tenant mapping use
the fallback. Fallback validation remains unchanged. Description metadata
is fetched in the existing per-language batches, not once per Page.

Legacy automatic templates lack a tenant column. Match only when the
template creator's server-authored managed-Page records identify exactly
one tenant; all matching Page configurations in that tenant are eligible.
No first-tenant assumption or cross-tenant lookup is allowed. Ambiguity
uses the automatic fallback. Caption version, source, reason and hashes
are frozen in each new task's selection receipt. Existing task copy,
publication states and uncertain receipts are never rewritten by deployment.

Deployment root: `/mnt/data-disk/fb-page-caption-20261113`.
Run `scripts/deploy_fb_page_caption.py prepare|apply|verify --folder
/mnt/data-disk/fb-page-caption-20261113/deployment` from the GitHub-verified
source. Preparation verifies the live baseline, tests an isolated candidate,
reads/render-checks all 236 captions as the actual FB service user, backs up
the ledger and narrowly patches the static help text. Activation drains
only FB work and restores the previous scheduler/coordinator states.

Rollback uses the same script with `rollback`. It restores the previous
runtime pointer/static text, never the ledger. Page-copy rollback is a
separate, version-checked configuration update based on `caption-update`
preimages; never restore the SQLite backup over live publication state.
