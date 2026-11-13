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

Concurrent deployment integration: the source-only caption changes are composed
onto explicitly hash-verified live core/repository/service files, preserving the
independent Moboreels promotion branch. Its m_url flow retains its own receipts.
The candidate also opens an already initialized metric database without DDL:
a long-running read-only metric migration held a shared lock that otherwise
blocked the API's repeated startup initialization. New metric databases still
initialize normally; existing readers validate the required tables.

Follow-up QA reproduced a selection bug: a valid Page caption without `{desc}`
could lose all materials because the automatic fallback required `{{desc}}`.
Material reads now share separate language/description-required pools; strict
fallback Pages retain their original filtered top-N. Exact-material runs permit
valid Pages and skip only Pages lacking required caption metadata. Neither path
can publish an empty required description. The publisher-path test uses a mock
transport and verifies that the frozen Page message reaches it unchanged.
`scripts/check_fb_page_caption.py` additionally reads the 236 approved live
configs and renders every enabled-template/Page pair in memory, with no network
publication, short-link allocation or production task writes.
