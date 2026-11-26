"""Permission-scoped TikTok income portraits on a single verified warehouse generation."""

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
from pathlib import Path
from subprocess import TimeoutExpired
import tempfile
import threading

from features.drama_audience.store import records as audience_records
from features.social_analytics.backend_scope import eligible
from features.social_analytics.bill_income import FIRST_DAY
from features.social_analytics.report import campaign_index, identity, resolve
from features.social_analytics.tiktok_scope import UNASSIGNED
from features.social_analytics.warehouse_reader import current, connect, DEFAULT_ROOT as WAREHOUSE
from features.x_tag_revenue.core import language
from features.x_tag_revenue.hierarchy import build_tree
from features.x_tag_revenue.taxonomy import classify, classification_index, collect_classifications, UNKNOWN


DEFAULT_ROOT = "/mnt/data-disk/tt-promotion/profiles"
WINDOWS = ("all", "7d", "28d")
_catalog_lock = threading.Lock()
_candidate_cache = {}


def _windows(manifest, last_complete_utc_day):
    source = "tiktok:2250"
    coverage = set(manifest.get("coverage", {}).get(source, []))
    if (
        not coverage
        or manifest.get("catalog_errors", {}).get(source)
        or manifest.get("catalog_errors", {}).get("tiktok")
    ):
        raise ValueError("tt_income_source_unavailable")
    first = date.fromisoformat(min(coverage))
    end = min(last_complete_utc_day, date.fromisoformat(manifest["end"]))
    if end < first:
        raise ValueError("tt_income_no_complete_day")
    iaa = set(manifest.get("iaa_coverage", {}).get(source, []))
    result = {}
    for period, start in [
        ("all", first),
        ("7d", end - timedelta(days=6)),
        ("28d", end - timedelta(days=27)),
    ]:
        days = {(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)}
        missing = sorted(days - coverage)
        missing_iaa = sorted(d for d in days if d >= FIRST_DAY and d not in iaa)
        result[period] = dict(
            start=start.isoformat(),
            end=end.isoformat(),
            missing_dates=missing,
            iaa_pending_dates=missing_iaa,
            status="partial" if missing or missing_iaa else "complete",
        )
    return result


def build_snapshot(catalog, effects, classifications, manifest, last_complete_utc_day):
    windows = _windows(manifest, last_complete_utc_day)
    end = windows["all"]["end"]
    catalog = [
        r
        for r in catalog
        if r["platform"] == "tiktok"
        and r["site_id"] == "2250"
        and not r.get("excluded")
        and eligible(r, end)
    ]
    index = campaign_index(catalog)
    owners = {key: resolve(candidates) for key, candidates in index.items()}
    social = defaultdict(int)
    targets = {}
    for key, candidates in index.items():
        row = owners[key]
        if (
            not row
            or not row["drama"]
            or not row["channel"]
            or row["owner"] == UNASSIGNED
            or not key[2].startswith("tt:")
            or row.get("platform_only")
        ):
            continue
        # The report resolves campaign ownership at drama level. Credit a material
        # only if every row for that exact campaign agrees on the frozen material.
        materials = set()
        for candidate in candidates:
            frozen = candidate.get("tt_identity") or []
            material = ""
            if (
                len(frozen) == 6
                and frozen[0] == candidate["owner"]
                and frozen[2] == candidate["channel"]
                and frozen[3] == candidate["drama"]
                and frozen[4]
                and candidate.get("tt_frozen_route", {}).get("key") == candidate["key"]
            ):
                material = str(frozen[4])
            materials.add(material)
        targets[key] = (
            row["tenant"],
            row["owner"],
            language(row["language"]),
            row["drama"],
            materials.pop() if len(materials) == 1 else "",
        )
    grouped = {}

    def accept(row, day, revenue=0, iaa=0, publications=0):
        if not row or not row["channel"] or not row["drama"]:
            return
        # normalized() adds a display/report platform prefix. Publisher channels use the stored account ID.
        channel = str(row["channel"]).partition(":")
        stable_id = channel[2] if channel[0] == "tiktok" and channel[1] else str(row["channel"])
        if not stable_id:
            return
        key = (stable_id, row["tenant"], row["owner"], row["drama"], language(row["language"]))
        item = grouped.setdefault(
            key,
            dict(
                channel=key[0],
                tenant=key[1],
                owner=key[2],
                content_id=key[3],
                language=key[4],
                name=row.get("drama_label") or key[3],
                windows={},
            ),
        )
        for period, info in windows.items():
            if info["start"] <= day <= end:
                totals = item["windows"].setdefault(period, [0, 0, 0])
                totals[0] += revenue
                totals[1] += iaa
                totals[2] += publications

    for metric in effects:
        if (
            metric.get("platform") != "tiktok"
            or str(metric["site_id"]) != "2250"
            or metric.get("tiktok_unassigned")
        ):
            continue
        day = metric["date"]
        key = ("tiktok", "2250", metric["campaign_id"])
        if day <= end and owners.get(key) and any(eligible(row, day) for row in index[key]):
            revenue, iaa = metric["revenue_cents"], metric.get("iaa_cents", 0)
            if type(revenue) is not int or type(iaa) is not int:
                raise ValueError("invalid_warehouse_cents")
            accept(owners[key], day, revenue, iaa)
            if key in targets and day >= windows["all"]["start"]:
                social[targets[key]] += revenue
    by_receipt = defaultdict(list)
    for key, candidates in index.items():
        for row in candidates:
            for fact in row.get("publications", []):
                by_receipt[fact["id"]].append((key, row, fact))
    for records in by_receipt.values():
        facts = {(identity(row), fact["date"], fact["kind"]) for _, row, fact in records}
        if len(facts) == 1 and all(owners.get(key) for key, _, _ in records):
            _, row, fact = records[0]
            accept(row, fact["date"], publications=1)
    categories = {cid: {} for cid, lang in classifications}
    for (cid, lang), data in classifications.items():
        categories[cid][lang] = data
    return dict(
        meta=dict(
            version=manifest["version"],
            generated_at=manifest["generated_at"],
            start=windows["all"]["start"],
            end=end,
            windows=windows,
            status=windows["all"]["status"],
        ),
        facts=list(grouped.values()),
        social_income=[
            dict(
                product="dramawave",
                tenant=tenant,
                owner=owner,
                language=lang,
                content_id=content,
                material_id=material,
                cents=cents,
            )
            for (tenant, owner, lang, content, material), cents in sorted(social.items())
        ],
        classifications=categories,
    )


def _catalog_labels(reader, ids):
    resources, infos = [], []
    for offset in range(0, len(ids), 25):
        batch = ",".join("0x" + cid.encode("utf-8").hex() for cid in ids[offset : offset + 25])
        for records, table, index, fields in [
            (
                resources,
                "ads_drama_resource",
                "content_id",
                ("content_id", "app_id", "language", "series_code", "labels"),
            ),
            (
                infos,
                "ads_drama_info",
                "ac",
                ("content_id", "app_id", "language", "series_code", "resource_type"),
            ),
        ]:
            args = ",".join("'" + field + "'," + field for field in fields)
            sql = (
                f"SELECT DISTINCT HEX(JSON_OBJECT({args})) FROM kunlunads_dev.{table} FORCE INDEX({index})"
                f" WHERE app_id=1479 AND content_id IN ({batch}) LIMIT 50001"
            )
            rows = reader.query([sql], timeout=60)
            if len(rows) >= 50001:
                raise ValueError("tt_drama_label_capacity")
            records.extend(rows)
    return classification_index(resources, infos, ids)


def refresh(root=None, warehouse=None, env_file="/root/drama_material_service/.env"):
    """Only the hourly job reads MySQL. Install the snapshot with one atomic rename."""
    import fcntl
    from features.social_analytics.warehouse_refresh import Reader, environment

    directory = Path(root or os.environ.get("TT_PROMOTION_PROFILE_ROOT") or DEFAULT_ROOT)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "refresh.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status="already_running")
        reader = Reader(environment(env_file))
        try:
            fans = reader.query(
                [
                    """SELECT HEX(JSON_OBJECT('id',CAST(source_account_id AS CHAR),
                    'followers',fan_count,'seen_at',DATE_FORMAT(last_seen_at,'%Y-%m-%d %H:%i:%s')))
                FROM ads_ai.tiktok_personal_account_snapshot
                WHERE is_active=1 AND has_metric_snapshot=1"""
                ],
                timeout=25,
            )
            _write_snapshot(
                directory,
                "followers.json",
                {
                    str(row["id"]): {"count": row["followers"], "seen_at": row["seen_at"]}
                    for row in fans
                    if type(row["followers"]) is int and row["followers"] >= 0
                },
            )
        except (OSError, RuntimeError, ValueError, KeyError, TimeoutExpired) as exc:
            logging.getLogger(__name__).warning(
                "TT follower snapshot refresh deferred: %s", type(exc).__name__
            )
        manifest, path = current(warehouse or os.environ.get("SOCIAL_ANALYTICS_WAREHOUSE_ROOT") or WAREHOUSE)
        last_day = datetime.now(timezone.utc).date() - timedelta(days=1)
        window = _windows(manifest, last_day)["all"]
        if window["status"] != "complete" and (directory / "current.json").is_file():
            prior = _snapshot(directory)
            if prior and prior["meta"]["status"] == "complete":
                raise ValueError("tt_income_refresh_pending")
        with closing(connect(path)) as db:
            catalog = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM catalog WHERE json_extract(payload,'$.platform')='tiktok'"
                )
            ]
            effects = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM effects WHERE platform='tiktok' AND dt<=?", (window["end"],)
                )
            ]
        ids = sorted({r["drama"] for r in catalog if r.get("drama")})
        classifications = _catalog_labels(reader, ids)
        data = build_snapshot(catalog, effects, classifications, manifest, last_day)
        _write_snapshot(directory, "current.json", data)
        return dict(status="updated", version=manifest["version"], dramas=len(ids), facts=len(data["facts"]))


def _write_snapshot(directory, filename, data):
    with tempfile.NamedTemporaryFile(
        "w", dir=directory, prefix=".profiles-", encoding="utf-8", delete=False
    ) as tmp:
        name = Path(tmp.name)
        try:
            json.dump(data, tmp, ensure_ascii=False, separators=(",", ":"))
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            tmp.close()
            name.unlink(missing_ok=True)
            raise
    try:
        name.chmod(0o640)
        os.replace(name, directory / filename)
    finally:
        name.unlink(missing_ok=True)


@lru_cache(maxsize=4)
def _cached(path, mtime, size, inode):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _snapshot(directory, filename="current.json"):
    path = directory / filename
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return _cached(str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)


def account_followers(root=None):
    directory = Path(root or os.environ.get("TT_PROMOTION_PROFILE_ROOT") or DEFAULT_ROOT)
    return _snapshot(directory, "followers.json") or {}


def social_metrics(actor, language_code, *, product="dramawave", snapshot=None, root=None):
    """Known cumulative 2250 income in USD cents for the actor's visible TT organic campaigns."""
    if snapshot is None:
        directory = Path(root or os.environ.get("TT_PROMOTION_PROFILE_ROOT") or DEFAULT_ROOT)
        try:
            snapshot = _snapshot(directory)
        except (OSError, ValueError):
            snapshot = None
    data = snapshot or {}
    meta = data.get("meta") or {}
    result = dict(
        status="unavailable",
        version=meta.get("version", ""),
        end=meta.get("end", ""),
        dramas={},
        materials={},
    )
    product = str(product or "").strip().lower()
    lang = language(language_code)
    if (
        not actor.get("tenant_key")
        or not actor.get("user_id")
        or product != "dramawave"
        or not lang
        or "social_income" not in data
    ):
        return result
    material_parent = defaultdict(set)
    for row in data["social_income"]:
        if (
            row["tenant"] != actor["tenant_key"]
            or (actor.get("role") != "admin" and row["owner"] != actor["user_id"])
            or row["product"] != product
            or row["language"] != lang
        ):
            continue
        content, material, cents = row["content_id"], row["material_id"], row["cents"]
        result["dramas"][content] = result["dramas"].get(content, 0) + cents
        if material:
            material_parent[material].add(content)
            result["materials"][material] = result["materials"].get(material, 0) + cents
    for material, parents in material_parent.items():
        if len(parents) > 1:
            del result["materials"][material]
    result["status"] = meta.get("status", "unavailable")
    if result["status"] == "partial":
        for scope in ("dramas", "materials"):
            result[scope] = {key: amount for key, amount in result[scope].items() if amount != 0}
    return result


def _merge_tree(dramas, classifications, window):
    def tree(field):
        values = {
            k: dict(
                content_id=k[0], language=k[1], name=d["name"], revenue_cents=d[field], posts=d["posts"]
            )
            for k, d in dramas.items()
        }
        return build_tree(values, classifications)["audiences"]

    audiences, iaa_tree = tree("revenue_cents"), tree("iaa_cents")
    ads = {a["key"]: a for a in iaa_tree}
    for a in audiences:
        a["iaa_cents"] = ads[a["key"]]["revenue_cents"]
        a["iap_cents"] = a["revenue_cents"] - a["iaa_cents"]
        by_theme = {t["key"]: t for t in ads[a["key"]]["children"]}
        for t in a["children"]:
            t["iaa_cents"] = by_theme[t["key"]]["revenue_cents"]
            t["iap_cents"] = t["revenue_cents"] - t["iaa_cents"]
            ad_drama = {
                (d["content_id"], d["language"]): d["allocated_cents"] for d in by_theme[t["key"]]["dramas"]
            }
            for d in t["dramas"]:
                d["iaa_cents"] = ad_drama[(d["content_id"], d["language"])]
                d["iap_cents"] = d["allocated_cents"] - d["iaa_cents"]
                d.pop("classification", None)
                _pending_amounts(d, window)
            _pending_amounts(t, window)
        _pending_amounts(a, window)
    return audiences


def _pending_amounts(row, window):
    """Only amounts actually returned may be presented as a partial subtotal."""
    if window["status"] == "complete":
        return
    if row.get("iaa_cents") == 0 and (window["iaa_pending_dates"] or window["missing_dates"]):
        row["iaa_cents"] = None
    if row.get("iap_cents") == 0 and window["missing_dates"]:
        row["iap_cents"] = None
    for field in ("revenue_cents", "allocated_cents"):
        if row.get(field) == 0:
            row[field] = None


def profiles_for_accounts(account_ids, actor, *, root=None, detail=False):
    ids = {str(value) for value in account_ids}
    directory = Path(root or os.environ.get("TT_PROMOTION_PROFILE_ROOT") or DEFAULT_ROOT)
    data = _snapshot(directory)
    if data is None:
        window = dict(
            status="unavailable",
            start="",
            end="",
            revenue_cents=None,
            iaa_cents=None,
            iap_cents=None,
            published_count=None,
            audiences=[],
        )
        return dict(
            meta=dict(status="unavailable", version="", start="", end="", generated_at="", stale=True),
            accounts={
                cid: dict(account_id=cid, status="unavailable", windows={k: dict(window) for k in WINDOWS})
                for cid in ids
            },
        )
    meta = dict(data["meta"])
    now = datetime.now(timezone.utc)
    age = now - datetime.fromisoformat(meta["generated_at"].replace("Z", "+00:00"))
    meta["stale"] = age.total_seconds() > 7200 or meta["end"] < (now.date() - timedelta(days=1)).isoformat()
    grouped = defaultdict(lambda: defaultdict(dict))
    for item in data["facts"]:
        channel = item["channel"]
        if channel not in ids:
            continue
        if not actor.get("_system_report") and (
            not actor.get("user_id")
            or not actor.get("tenant_key")
            or actor["tenant_key"] != item["tenant"]
            or (actor.get("role") != "admin" and actor["user_id"] != item["owner"])
        ):
            continue
        key = (item["content_id"], item["language"])
        for period, (total, iaa, posts) in item["windows"].items():
            d = grouped[channel][period].setdefault(
                key, dict(name=item["name"], revenue_cents=0, iaa_cents=0, posts=set())
            )
            d["revenue_cents"] += total
            d["iaa_cents"] += iaa
            # Counts were de-duplicated across owners and resolutions before freezing.
            d["posts"].update((key, item["tenant"], item["owner"], i) for i in range(posts))
    accounts = {}
    for channel in ids:
        windows = {}
        for period, info in meta["windows"].items():
            dramas = grouped[channel][period]
            categories = {
                key: data["classifications"][key[0]][key[1]]
                for key in dramas
                if key[1] in data["classifications"].get(key[0], {})
            }
            # Use the catalog as frozen at refresh, including missing classifications.
            for key in dramas.keys() - categories.keys():
                categories[key] = classify([], status="missing_drama_info", expected_language=key[1])
            audiences = _merge_tree(dramas, categories, info) if dramas else []
            if not detail:
                for a in audiences:
                    for t in a["children"]:
                        t.pop("dramas", None)
            total = sum(d["revenue_cents"] for d in dramas.values())
            iaa = sum(d["iaa_cents"] for d in dramas.values())
            windows[period] = dict(
                info,
                revenue_cents=total,
                iaa_cents=iaa,
                iap_cents=total - iaa,
                published_count=sum(len(d["posts"]) for d in dramas.values()),
                audiences=audiences,
            )
            _pending_amounts(windows[period], info)
            if info["status"] != "complete" and not dramas:
                windows[period].update(
                    revenue_cents=None, iaa_cents=None, iap_cents=None, published_count=None
                )
        status = (
            "partial"
            if windows["all"]["status"] != "complete"
            else "available"
            if (windows["all"]["revenue_cents"] or 0) > 0
            else "no_positive_revenue"
        )
        accounts[channel] = dict(
            account_id=channel, version=meta["version"], status=status, windows=windows
        )
    return dict(meta=meta, accounts=accounts)


def _weighted(nodes, seed):
    scores = []
    for node in nodes:
        if not node.get("matchable") or node["revenue_cents"] <= 0:
            continue
        val = int.from_bytes(hashlib.sha256((seed + "\0" + node["key"]).encode()).digest()[:8], "big")
        sample = (val + 1) / (2**64 + 1)
        scores.append((-math.log(sample) / node["revenue_cents"], node["key"]))
    return [key for score, key in sorted(scores)]


def _classifications(items, version, root, source):
    data = _snapshot(Path(root or os.environ.get("TT_PROMOTION_PROFILE_ROOT") or DEFAULT_ROOT)) or {}
    result = {}
    missing = {}
    for item in items:
        cid, lang = (
            str(item.get("content_id", "")),
            language(item.get("language") or item.get("drama_language")),
        )
        tags = item.get("classification") or data.get("classifications", {}).get(cid, {}).get(lang)
        if tags:
            result[(cid, lang)] = tags
        else:
            missing[cid, lang] = item
    if missing and source is not None:
        cache_key = (version, source.schema)
        with _catalog_lock:
            result.update((k, v) for k, v in _candidate_cache.get(cache_key, {}).items() if k in missing)
        unresolved = set(missing) - result.keys()
        if unresolved:
            found = collect_classifications(
                source._stream_select, source.schema, sorted({k[0] for k in unresolved})
            )
            with _catalog_lock:
                if cache_key not in _candidate_cache:
                    _candidate_cache.clear()
                cached = _candidate_cache.setdefault(cache_key, {})
                if len(cached) > 20000:
                    cached.clear()
                cached.update(found)
            result.update(found)
    unresolved = set(missing) - result.keys()
    records = audience_records([k[0] for k in unresolved]) if unresolved else {}
    for cid, lang in unresolved:
        result[cid, lang] = classify(
            missing[cid, lang].get("labels", []), audience_record=records.get(cid), expected_language=lang
        )
    return result


def match_drama(profile, content_id, language_code, *, root=None, source=None):
    """Conditional weights for binding a first episode to a same-language channel."""
    if not profile or profile.get("status") != "available":
        return (0, 0)
    key = (str(content_id), language(language_code))
    values = _classifications(
        [dict(content_id=key[0], language=key[1])], profile.get("version"), root, source
    )[key]
    for a in profile["windows"]["all"]["audiences"]:
        if a["matchable"] and a["key"] == values["audience"]:
            theme = max(
                (
                    t["share_bp"] or 0
                    for t in a["children"]
                    if t["matchable"] and t["key"] in values["themes"]
                ),
                default=0,
            )
            return (a["share_bp"] or 0, theme)
    return (0, 0)


def rank_candidates(candidates, profile, seed, *, root=None, source=None):
    items = list(candidates)
    profile = profile or {}
    evidence = dict(
        profile_version=profile.get("version", ""),
        preferred_audience="",
        preferred_theme="",
        selected_audience="",
        selected_theme="",
        reason="",
    )
    if not items or profile.get("status") != "available":
        evidence["reason"] = (
            "no_eligible_material" if not items else profile.get("status") or "no_positive_revenue"
        )
        return items, evidence
    nodes = profile["windows"]["all"]["audiences"]
    seed = json.dumps(
        [str(seed), profile["account_id"], evidence["profile_version"]], separators=(",", ":")
    )
    audiences = _weighted(nodes, seed)
    if not audiences:
        evidence["reason"] = "no_valid_audience_profile"
        return items, evidence
    topics = {n["key"]: _weighted(n["children"], seed + "\0" + n["key"]) for n in nodes}
    try:
        catalog = _classifications(items, profile.get("version"), root, source)
    except (RuntimeError, OSError, ValueError) as exc:
        evidence["reason"] = "candidate_classification_unavailable"
        evidence["error_type"] = type(exc).__name__
        return items, evidence
    evaluated = []
    for pos, item in enumerate(items):
        cid, lang = (
            str(item.get("content_id", "")),
            language(item.get("language") or item.get("drama_language")),
        )
        tags = catalog[cid, lang]
        a = tags.get("audience", UNKNOWN)
        theme_order = topics.get(a, []) if a in audiences else []
        theme = next((t for t in theme_order if t in tags.get("themes", ())), None)
        evaluated.append(
            (
                (
                    audiences.index(a) if a in audiences else len(audiences),
                    theme_order.index(theme) if theme else len(theme_order),
                    pos,
                ),
                item,
                a,
                theme or (tags.get("themes") or [UNKNOWN])[0],
            )
        )
    evaluated.sort(key=lambda entry: entry[0])
    best = evaluated[0]
    primary = audiences[0]
    first_theme = next(iter(topics.get(primary, [])), "")
    reason = (
        "preferred_audience_unavailable"
        if best[2] != primary
        else "no_valid_theme_profile"
        if not first_theme
        else "preferred_theme_unavailable"
        if best[3] != first_theme
        else "preferred_match"
    )
    evidence.update(
        preferred_audience=primary,
        preferred_theme=first_theme,
        selected_audience=best[2],
        selected_theme=best[3],
        reason=reason,
    )
    return [r[1] for r in evaluated], evidence


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["refresh"])
    parser.add_argument("--root")
    parser.add_argument("--warehouse")
    parser.add_argument("--env", default="/root/drama_material_service/.env")
    args = parser.parse_args()
    print(json.dumps(refresh(args.root, args.warehouse, args.env)))
