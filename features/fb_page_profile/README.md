# Facebook Page paid-effect portraits

One-time report for the frozen 236 Pages in group 62. The user revised the
requirement to **only the latest seven complete report dates** and one Page per
table row, following the current X channel audience/theme presentation. No
historical fallback, scheduled refresh, publisher changes, or Page-language writes.

## Input and calculation

`build.py` takes a frozen JSON extract containing `scope`, `membership`, `pages`,
`windows`, `effects`, `resources`, `drama_info`, `geo`, `start`, and `end`.
Use the read-only 63350 endpoint and verify `@@read_only=1`. The source is
`kunlunads_dev.ads_custom_source_insight`, with exact `product='Dramawave'`,
`platform=0`, an inclusive seven-day `dt` interval, and the frozen Page IDs.
Aggregate by Page, day, `series_code`, `data_source_id`, and
`COALESCE(NULLIF(drama_language,''),NULLIF(language,''),'unknown')`.
Keep spend, D0 income, cumulative attribution income, platform income, impressions,
clicks, installs, purchases, and source row count separate. Never add overlapping
insight tables or historical extracts to the input.

Current group-language and Page status fields exclude access tokens. Content labels
are DISTINCT resource records for app 1479, exact content ID and language; drama
metadata uses the same product/version key. The shared X semantic dictionary and
taxonomy are reused from `features.x_tag_revenue`, with no edits to that feature.
The deployed 2026-11-10 two-level hierarchy is followed: audience, then distinct
broad themes. Revenue and spend are equally allocated over unique themes and
rounded with exact integer-cent conservation. Conditional shares are rounded to
basis points and sum to 100% for each valid parent.

The list shows all content-audience shares of Page D0 income. Its theme column
shows the largest audience's children as shares **within that audience**, with
three leading themes and an explicit remaining subtotal. Zero-spend Pages retain
their source monetary facts but have `profile_eligible=false` and no selected focus.
Content directions are not demographic or sensitive-attribute claims about people.
Amounts retain source-report units; independent currency verification is not claimed.

## Build and verify

```sh
python3 -m unittest tests.test_fb_page_profile -v
python3 -m features.fb_page_profile.build raw.json snapshot.json
python3 -m features.fb_page_profile.render snapshot.json index.html
```

Reconcile Page totals against a separate Page-level query. Verify exact 236 IDs,
20 blank-language backups, common dates, total money, audience/theme share sums,
and current group membership. Browser verification covers 236 rows, language
filter, 139 no-spend rows for this snapshot, row detail, search, export, and mobile
horizontal table scrolling. The HTML embeds a compact projection: first 50 dramas
per Page, original totals and precomputed full language shares. Full provenance
stays in the database. No external scripts or fonts are required.

## Authorized publication

The user expressly requested a new ads_ai table and COS HTML publication. Deploy
the verified GitHub commit into an isolated data-disk directory; no service restart.
Review `schema.sql` and verify the target table does not exist before first creation.
The writer permits only the named table, requires an explicit `--create-table`,
checks `DATABASE()='ads_ai'` and `@@read_only=0`, and inserts one immutable version
in a transaction. All data/schema exploration and readback use 63350. Credentials
come from the existing server `.env` in process memory and are never committed.

```sh
python3 -m features.fb_page_profile.publish --snapshot snapshot.json \
  --html index.html --receipt receipt.json --create-table
```

Database readback compares SHA-256 of every full JSON row, row count, and cent totals.
An existing same-version result must match completely; partial/conflicting outcomes
fail rather than overwrite. COS uses a unique versioned key with a hash metadata
header and readback. Retry only after reconciling receipts. Verify the resulting
public URL and HTML content separately.

## Scope and rollback

First installation has no previous table or public object to replace. Retain raw
extract, snapshot, source commit and receipt under
`/mnt/data-disk/fb-page-effect-profile-20261110/`. Existing Page configuration,
publishing services, X tables and schedules are untouched. Rollback means withdrawing
this version's COS object and, only if requested, deleting its rows using a reviewed
exact `profile_version` plus `group_id=62` predicate. Never drop the table or remove
another version. The isolated source tree can be left inert; no restart is needed.
