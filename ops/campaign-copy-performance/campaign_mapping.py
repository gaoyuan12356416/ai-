"""Refresh exact Ad membership using covering indexes; rotate descriptive fields."""
from __future__ import annotations

import collections
import base64
import datetime as dt
import hashlib
import json
import time


MEMBERSHIP_BATCH = 5000
DETAIL_BATCH = 250
METADATA_BUCKETS = 96  # One complete rotation at a 15 minute successful cadence.
METADATA_MAX_AGE = dt.timedelta(days=1)
METADATA_FIELDS = ("campaign_name", "product", "countries", "languages")
SOURCE_SPECS = (
    (0, "kunlunads_dev.ads_facebook_auto_created_data", "campaign_id", "product"),
    (0, "ads_ai.ads_facebook_auto_created_data", "campaign_id", "product"),
    (3, "kunlunads_dev.ads_tiktok_auto_created_data", "campaign_id_2", "'' AS product"),
)


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def timestamp(value):
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def membership_signature(source, rows):
    # Include duplicates, source precedence, and empty/invalid IDs in the digest.
    # Metric queries always use the newly read exact IDs, never this digest.
    ids = sorted(str(row.get("ad_id") or "") for row in rows)
    digest = hashlib.sha256(json.dumps([source, ids], separators=(",", ":")).encode()).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")


def metadata_bucket(platform, campaign_id):
    return int(hashlib.sha256(f"{platform}:{campaign_id}".encode()).hexdigest()[:8], 16) % METADATA_BUCKETS


def dominant(values):
    counts = collections.Counter(str(value).strip() for value in values if str(value or "").strip())
    return sorted(counts, key=lambda value: (-counts[value], value))[0] if counts else ""


def joined(values):
    items = sorted({str(value).strip() for value in values if str(value or "").strip()})
    return "、".join(items[:4]) + (f" 等{len(items)}项" if len(items) > 4 else "")


def query_campaign_mapping(cur, logs, previous, now):
    """Keep current membership/fallback semantics without a million table lookups.

    New Campaigns and changed membership get fresh descriptions immediately.
    Unchanged descriptions are read in 96 rotating buckets, at most 24h old.
    The published report is the only seed, so a failed refresh cannot advance it.
    """
    started = time.monotonic()
    prior = {}
    if previous:
        fields = previous.get("cf", [])
        wanted = {name: fields.index(name) for name in (
            "platform", "campaign_id", "ad_count", "mapped", *METADATA_FIELDS,
            "metadata_signature", "metadata_checked_at", "metadata_product",
        ) if name in fields}
        for values in previous.get("c", []):
            row = {name: values[index] for name, index in wanted.items()}
            # Report product may come from the log's App name. It is not the
            # raw mapping product and cannot be reused as that fallback.
            row.pop("product", None)
            if row.get("metadata_product") is not None:
                row["product"] = row["metadata_product"]
            elif int(row.get("platform") or 0) == 3:
                row["product"] = ""
            prior[(int(row.get("platform") or 0), str(row["campaign_id"]))] = row
    now = timestamp(now)
    if now is None:
        raise ValueError("mapping snapshot time unavailable")
    previous_meta = (previous or {}).get("m", {})
    seed_time = timestamp(previous_meta.get("generated_at"))
    bucket = int(previous_meta.get("mapping_metadata_next_bucket", 0)) % METADATA_BUCKETS
    ids_by_platform = collections.defaultdict(set)
    for row in logs:
        ids_by_platform[int(row.get("platform") or 0)].add(str(row["new_id"]))

    mapping = []
    grouped = collections.defaultdict(list)
    sources = {}
    metadata = {}
    stats = {"membership_queries": 0, "detail_queries": 0, "detail_campaigns": 0,
             "membership_rows": 0, "detail_rows": 0, "reused_campaigns": 0}
    for platform, table, index, product_sql in SOURCE_SPECS:
        # Meta's second table is a fallback per Campaign, not a union.
        remaining = sorted(cid for cid in ids_by_platform[platform] if (platform, cid) not in sources)
        for part in chunks(remaining, MEMBERSHIP_BATCH):
            placeholders = ",".join(["%s"] * len(part))
            cur.execute(
                f"SELECT campaign_id, ad_id FROM {table} FORCE INDEX ({index}) "
                f"WHERE campaign_id IN ({placeholders})", part,
            )
            stats["membership_queries"] += 1
            for row in cur.fetchall():
                cid = str(row["campaign_id"])
                row["platform"] = platform
                mapping.append(row)
                grouped[(platform, cid)].append(row)
                sources[(platform, cid)] = table

        refresh_ids = []
        for cid in remaining:
            key = (platform, cid)
            if sources.get(key) != table:
                continue
            rows = grouped[key]
            signature = membership_signature(table, rows)
            old = prior.get(key)
            checked = timestamp((old or {}).get("metadata_checked_at")) or seed_time
            # The v2 migration reuses verified labels once when membership count
            # matches; exact signatures apply on every subsequent refresh.
            old_signature = (old or {}).get("metadata_signature")
            same = old_signature == signature if old_signature else bool(
                old and old.get("mapped") and old.get("ad_count") == len(
                    {str(r["ad_id"]) for r in rows if r.get("ad_id")}
                )
            )
            due = (not same or checked is None or checked > now or now - checked >= METADATA_MAX_AGE
                   or metadata_bucket(platform, cid) == bucket
                   or any(field not in (old or {}) for field in METADATA_FIELDS))
            metadata[key] = {"metadata_signature": signature,
                             "metadata_checked_at": str(now if due else checked)}
            if due:
                refresh_ids.append(cid)
            else:
                metadata[key].update({field: old[field] for field in METADATA_FIELDS})
                stats["reused_campaigns"] += 1

        for part in chunks(refresh_ids, DETAIL_BATCH):
            placeholders = ",".join(["%s"] * len(part))
            # status, budget and ad_account_id were fetched but never consumed.
            cur.execute(
                f"SELECT campaign_id, ad_id, campaign_name, {product_sql}, country, language "
                f"FROM {table} FORCE INDEX ({index}) WHERE campaign_id IN ({placeholders})", part,
            )
            stats["detail_queries"] += 1
            details = collections.defaultdict(list)
            for row in cur.fetchall():
                details[str(row["campaign_id"])].append(row)
                stats["detail_rows"] += 1
            for cid in part:
                key = (platform, cid)
                rows = details[cid]
                if membership_signature(table, rows) != metadata[key]["metadata_signature"]:
                    raise RuntimeError("Campaign membership changed within the source snapshot")
                metadata[key].update(
                    campaign_name=dominant(r.get("campaign_name") for r in rows),
                    product=dominant(r.get("product") for r in rows),
                    countries=joined(r.get("country") for r in rows),
                    languages=joined(r.get("language") for r in rows),
                )
            stats["detail_campaigns"] += len(part)

    # Empty lookups must clear old labels and must never remove an entity from
    # the copy-log population. Newly arriving children are detected next cycle.
    for platform, ids in ids_by_platform.items():
        for cid in ids:
            key = (platform, cid)
            if key not in metadata:
                metadata[key] = {field: "" for field in METADATA_FIELDS}
                metadata[key].update(metadata_signature=membership_signature("", []),
                                     metadata_checked_at=str(now))
    stats.update(membership_rows=len(mapping), seconds=round(time.monotonic() - started, 3))
    print(json.dumps({"event": "campaign_copy_report_mapping", **stats}), flush=True)
    return mapping, metadata, (bucket + 1) % METADATA_BUCKETS, stats
