# Shared social analytics

`routes.build_report(app, actor, query, include_titles=False, export=False)` is
the single collector used by HTTP, CSV and the scheduled publisher report.
`app` is a plain settings dictionary; importing the collector never starts a
publisher, initializes a database, or imports `app.py`.

## Input and access

- `JOB_DB_PATH` contains the existing YouTube publishing/link ledgers.
- `SOCIAL_ANALYTICS_X_DB_PATH` defaults to
  `/var/lib/x-post-automation/accounts.sqlite3`.
- `ADMIN_MAPPING_MYSQL_*` use the existing read-only `101.32.56.53:63350`
  account. Every read verifies `@@read_only=1`, uses a read-only transaction
  and UTC session, has bounded execution and row limits, and retains SQL gate
  enforcement. Credentials never appear in arguments or returned errors.
- `query` has `parse_qs` shape. `platform=youtube&platform=x` selects both
  channels; omitted platform selects every registered channel. Default dates
  are the last seven completed UTC days and default grouping is
  `owner,platform`. At most three grouping dimensions are accepted.
- `actor` has trusted `tenant_key`, `user_id`, `role`. Normal users see their
  own frozen ownership; admins see their tenant. HTTP strips any extra actor
  fields. The scheduled collector alone can pass `_system_report=True`;
  this capability must never be accepted from requests or session content.
- Legacy dispatch is `dispatch(handler, parsed, app, legacy=True)`. It forces
  YouTube, preserves the old default owner grouping and accepts old unprefixed
  channel IDs. The new API prefixes channel values with `youtube:` or `x:`.

## Attribution and quality

YouTube and X match the campaign ID against the frozen link's `af_c_id`
within the platform and site. Facebook also checks the frozen Page and material.
TikTok site 2250 matches the case-sensitive pair of campaign ID and full campaign
name against the recorded link and a confirmed public post in either TikTok ledger.
YouTube's known 32-character storage alias is accepted only when globally
unique. Conflict resolution precedes tenant visibility and every user filter.
Identical duplicated ledger identities do not multiply effects; conflicting
identities remain unmatched.

All integer metrics and cents are aggregated before formatting. Report DTOs
retain `revenue_cents` and `refund_cents` as well as display dollars. The
scheduled report can use integer cents without introducing float rounding.

`all_rows`, `rows`, `totals`, `platform_totals`, `trend` and `ranking` share the
same aggregation. Owner grouping also returns `owner_tenant`, and system
reports group identical user IDs from different tenants separately. Counts
are `links`, `published_count`, `post_count`, `repost_count`; X counts only
confirmed target publications, never its relay source. Manual YouTube links
contribute effects and generated links but no video publication count.

`quality.platforms[platform]` contains independent `state`, `missing_dates`,
`error`, source/attributed/unmatched/excluded totals and reconciliation scope.
`quality.complete` describes the selected channels and dates. DTO
`data_status` is `complete`, `partial` or `unavailable`. Missing metrics are
null, partial totals contain only available source data, and publication
counts remain usable when their ledger is readable but the effect source is
unavailable. Missing attribution ledgers also make publication counts null.

Only the trusted system collector receives the complete global source
reconciliation. A tenant admin can see conflicting amounts only if **all**
candidate identities belong to that tenant. Outside the explicitly bound
TikTok site described below, a campaign with no candidate
cannot safely be assigned to a tenant and is therefore exposed only to the
system collector. This prevents unmatched-data totals from leaking another
tenant's amounts. System totals enforce, field by field:

`source = attributed + unmatched + explicitly excluded`

### TikTok history and daily reports

The hourly warehouse records site 2250 under `social_analytics_tiktok_daily`.
Historical clicks, visits, installs, conversions and refunds use the UTC
site insights. Recharge and IAA amounts use exact daily source totals and
deterministic cent allocation across campaign pairs. The first verified
insight date is 2026-07-31. IAA coverage begins with received ad events.

The reporting tenant is bound with `SOCIAL_ANALYTICS_TT_SITE_TENANT`.
That tenant's administrators see site totals including the generic 0001
entry and revenue awaiting attribution. Ordinary users see confirmed
personal performance. Group by owner to inspect the unassigned portion.
The 11:00 publisher report shows both portions. The 11:30 gap report
compares confirmed drama identities and marks unresolved comparisons pending.

## Future channels

Add a verified channel/site entry to `adapters.PLATFORMS` and a read-only
catalog reader in `read_catalogs`. A reader returns normalized immutable
identity rows and confirmed publication facts. Source metrics and all
aggregation, filtering, CSV and daily reporting then use the common path.
Verify a new channel's site, stored campaign identity and ledger contract
before registering it. The truncation rule is specific to YouTube.

## Validation

```text
python -m unittest discover -s tests -p test_social_analytics.py -v
python -m py_compile features/social_analytics/adapters.py features/social_analytics/report.py features/social_analytics/routes.py
```

Tests cover real temporary SQLite catalogs, case-sensitive/cross-channel
campaign resolution, alias conflicts, one-to-many deduplication, ACLs and
filter ordering, full-source conservation, missing data, known publication
counts, manual links, CSV escaping, read-only enforcement and legacy routes.
