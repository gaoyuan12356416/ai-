#!/usr/bin/env python3
"""Synchronize reviewed FB delivery products into the V3 ads_ai catalog.

The default mode is read-only. ``--apply`` requires an exact expected row count
and plan hash so a changed source catalog cannot be written accidentally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import Any, Dict, Iterable, List, Mapping, Sequence


CATALOG_TABLE = "`ads_ai`.`ad_control_v3_product_catalog`"


def _clean_list(values: Iterable[Any]) -> List[str]:
    return sorted({str(value or "").strip() for value in values if str(value or "").strip()})


def _landing_names(raw: Any) -> List[str]:
    if isinstance(raw, Mapping):
        payload = raw
    else:
        try:
            payload = json.loads(str(raw or "{}"))
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, Mapping):
        return []
    return _clean_list(payload.values())


def build_catalog_entries(
    rows: Sequence[Mapping[str, Any]],
    *,
    platform_app_id: str,
    canonical_product: str,
    insight_products: Sequence[str],
) -> List[Dict[str, Any]]:
    exact_products = _clean_list(insight_products)
    if not platform_app_id or not canonical_product or not exact_products:
        raise ValueError("platform_app_id, canonical_product and insight_products are required")
    entries: List[Dict[str, Any]] = []
    seen = set()
    for source in rows:
        platform_product_id = int(source.get("id") or 0)
        landing_id = int(source.get("landing_id") or 0)
        source_app_id = str(source.get("app_id") or "").strip()
        if platform_product_id <= 0 or source_app_id != platform_app_id:
            raise ValueError("source row does not match the reviewed platform app id")
        platform_name = str(source.get("name") or "").strip() or ("产品 %s" % platform_product_id)
        landing_names = _landing_names(source.get("landing_app"))
        if landing_id > 0:
            product_value = "w2a:%s" % landing_id
            display_name = "%s · W2A %s" % ((" / ".join(landing_names) or canonical_product), landing_id)
            insight_app_ids = ["[w2a]%s" % name for name in landing_names]
            w2a_page_ids = [landing_id]
        else:
            product_value = "app:%s" % platform_product_id
            display_name = "%s · App %s" % (platform_name, platform_product_id)
            insight_app_ids = [platform_name]
            w2a_page_ids = []
        if product_value in seen:
            raise ValueError("duplicate delivery product selector: %s" % product_value)
        seen.add(product_value)
        evidence = {
            "catalog_kind": "delivery_product",
            "display_name": display_name,
            "description": "%s · Facebook App ID %s" % (platform_name, platform_app_id),
            "platform_app_id": platform_app_id,
            "platform_product_id": platform_product_id,
            "platform_product_name": platform_name,
            "scope": {
                "insight_products": exact_products,
                "insight_app_ids": _clean_list(insight_app_ids),
                "w2a_page_ids": w2a_page_ids,
            },
            "source": "ads_apps_setting_app_id_batch",
            "verified_on": "2026-07-17",
        }
        entries.append({
            "channel": "facebook",
            "product_value": product_value,
            "canonical_product": canonical_product,
            "product_type": "short_drama",
            "source_app_ids": [platform_product_id],
            "evidence": evidence,
        })
    return sorted(entries, key=lambda item: item["product_value"])


def plan_hash(entries: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(list(entries), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError("missing environment: %s" % name)
    return value


def _connection(prefix: str, database: str, *, autocommit: bool):
    import pymysql

    return pymysql.connect(
        host=_required_env(prefix + "_HOST"),
        port=int(_required_env(prefix + "_PORT")),
        user=_required_env(prefix + "_USER"),
        password=str(os.environ.get(prefix + "_PASSWORD") or ""),
        database=database,
        charset="utf8mb4",
        connect_timeout=3,
        read_timeout=10,
        write_timeout=10,
        autocommit=autocommit,
        cursorclass=pymysql.cursors.DictCursor,
    )


def load_source_rows(platform_app_id: str) -> List[Dict[str, Any]]:
    connection = _connection("AD_CONTROL_V3_SOURCE_READER_MYSQL", "kunlunads_dev", autocommit=True)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT id,name,app_id,app_type,landing_id,landing_app,ads_app_id,pause_ad "
            "FROM `kunlunads_dev`.`ads_apps_setting` FORCE INDEX (`app_id`) "
            "WHERE app_id=%s ORDER BY id",
            (platform_app_id,),
        )
        return [dict(row) for row in (cursor.fetchall() or [])]
    finally:
        connection.close()


def apply_entries(entries: Sequence[Mapping[str, Any]]) -> None:
    connection = _connection("AD_CONTROL_V3_STORE_WRITER_MYSQL", "ads_ai", autocommit=False)
    sql = (
        "INSERT INTO %s "
        "(channel,product_value,canonical_product,product_type,source_app_ids_json,evidence_json,enabled,created_by_user_id,updated_by_user_id,created_at,updated_at) "
        "VALUES (%%s,%%s,%%s,%%s,%%s,%%s,1,'delivery-product-sync','delivery-product-sync',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) "
        "ON DUPLICATE KEY UPDATE canonical_product=VALUES(canonical_product),product_type=VALUES(product_type),"
        "source_app_ids_json=VALUES(source_app_ids_json),evidence_json=VALUES(evidence_json),enabled=1,"
        "updated_by_user_id='delivery-product-sync',updated_at=UTC_TIMESTAMP(6)"
    ) % CATALOG_TABLE
    try:
        cursor = connection.cursor()
        for item in entries:
            cursor.execute(sql, (
                item["channel"], item["product_value"], item["canonical_product"], item["product_type"],
                json.dumps(item["source_app_ids"], ensure_ascii=False, separators=(",", ":")),
                json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-app-id", required=True)
    parser.add_argument("--canonical-product", required=True)
    parser.add_argument("--insight-product", action="append", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--expected-hash")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    entries = build_catalog_entries(
        load_source_rows(args.platform_app_id),
        platform_app_id=args.platform_app_id,
        canonical_product=args.canonical_product,
        insight_products=args.insight_product,
    )
    digest = plan_hash(entries)
    print(json.dumps({"count": len(entries), "plan_hash": digest, "apply": bool(args.apply)}, ensure_ascii=False))
    if args.apply:
        if args.expected_count is None or not args.expected_hash:
            raise RuntimeError("--apply requires --expected-count and --expected-hash")
        if len(entries) != args.expected_count or digest != args.expected_hash:
            raise RuntimeError("source catalog changed; refusing to apply")
        apply_entries(entries)
        print(json.dumps({"written": len(entries), "plan_hash": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
