"""Read-only business-source adapter; all queries use the existing SQL FIFO gate."""
import re
from .core import AssetError, account_id, content_markers, stored_ids, stored_ids_complete


def q(value):
    # A charset-introduced hex literal stays coercible (4), so the source
    # column's collation wins. CONVERT(...) has implicit coercibility (2) and
    # fails on the live mix of unicode_ci/general_ci columns (MySQL 1267).
    # Hex also avoids SQL-mode-dependent string escaping.
    return "_utf8mb4 0x%s" % str(value).encode("utf-8").hex() if str(value) else "''"


def inside(values):
    return "(" + ",".join(q(x) for x in values) + ")" if values else "(NULL)"


def chunks(values, size=250):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


class SqlSource:
    AD_COLUMNS = ("row_id", "product_id", "ad_id", "creative_id", "video_ids_raw", "source_ids_raw", "original_ids_raw", "account_id", "user_id", "campaign_id", "ad_name", "campaign_name", "local_status")

    def __init__(self, query, schema="kunlunads_dev", lookup_actor=None):
        if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
            raise ValueError("invalid source schema")
        self.query = query
        self.schema = "`%s`" % schema
        self.lookup_actor = lookup_actor or (lambda session: {})

    def read(self, sql, columns, timeout=30):
        if not sql.lstrip().upper().startswith("SELECT"):
            raise AssetError("read_only_source", "业务源库仅允许读取", 500)
        try:
            rows = self.query(" ".join(sql.split()), timeout)
        except Exception:
            raise AssetError("source_unavailable", "业务数据查询未完成，请缩小范围或稍后重试；此次不能执行删除", 503) from None
        return [dict(zip(columns, row)) for row in rows]

    def list_products(self, session):
        permission = "1=1"
        if session.get("role") != "admin":
            group = self.lookup_actor(session) or {}
            sub_id = str(group.get("sub_user_id") or "")
            if not sub_id.isdigit():
                return []
            permission = """EXISTS (SELECT 1 FROM {s}.admin_role_users aru
                JOIN {s}.admin_role_apps ara ON ara.id=aru.role_app_id
                WHERE aru.user_id={uid} AND (ara.is_all=1 OR FIND_IN_SET(CAST(a.id AS CHAR),REPLACE(ara.values,' ',''))))""".format(s=self.schema, uid=q(sub_id))
        rows = self.read("""SELECT CAST(a.id AS CHAR),a.name,CAST(a.app_type AS CHAR),
            CAST(CASE WHEN a.app_type=14 THEN a.id ELSE COALESCE(p.id,0) END AS CHAR),
            CASE WHEN a.app_type=14 THEN a.name ELSE COALESCE(p.name,'') END,
            CAST(COALESCE(a.default_user,0) AS CHAR)
            FROM {s}.ads_apps_setting a LEFT JOIN {s}.ads_apps_setting p ON p.id=a.ads_app_id AND p.app_type=14
            WHERE (a.app_type=14 OR (a.app_type IN (10000,10001) AND (a.landing_app_type=14 OR p.id IS NOT NULL)))
            AND ({permission}) ORDER BY a.name,a.id LIMIT 5001""".format(s=self.schema, permission=permission),
            ("id", "name", "app_type", "parent_id", "parent_name", "default_user"))
        if len(rows) > 5000:
            raise AssetError("too_many_products", "短剧产品目录超出可读取范围", 503)
        for row in rows:
            row["kind"] = "App" if str(row.pop("app_type")) == "14" else "W2A"
            row["id"] = str(row["id"])
            row["parent_id"] = str(row["parent_id"])
        return rows

    def selected_products(self, session, ids):
        allowed = {row["id"]: row for row in self.list_products(session)}
        if any(pid not in allowed for pid in ids):
            raise AssetError("product_permission_denied", "所选产品不属于可访问的短剧投放产品", 403)
        return [allowed[pid] for pid in ids]

    def resolve_dramas(self, input_type, ids, products):
        parent_ids = sorted({p["parent_id"] for p in products if p["parent_id"] not in ("", "0")})
        rows = []
        if parent_ids:
            rows = self.read("SELECT CAST(app_id AS CHAR),content_id,series_code,language,name FROM %s.ads_drama_info WHERE app_id IN %s AND %s IN %s ORDER BY id LIMIT 50001" %
                (self.schema, inside(parent_ids), input_type, inside(ids)), ("parent_id", "content_id", "series_code", "language", "name"))
        if len(rows) > 50000:
            raise AssetError("too_many_drama_versions", "资源展开超过上限，请缩小输入范围")
        grouped = {}
        for row in rows:
            if input_type == "series_code":
                row["series_code"] = str(row["series_code"]).upper()
            grouped.setdefault((str(row["parent_id"]), str(row["content_id"])), []).append(row)
        dramas, blockers = [], []
        for product in products:
            found_inputs = set()
            for (parent, content), versions in grouped.items():
                if parent != product["parent_id"]:
                    continue
                matched = str(versions[0][input_type])
                if matched not in ids:
                    continue
                found_inputs.add(matched)
                identities = {(v["series_code"], str(v["language"]).lower()) for v in versions}
                if len(identities) != 1:
                    blockers.append(dict(code="ambiguous_drama", message="同一剧 ID 的资源或语言记录冲突", product_id=product["id"], input_id=matched))
                    continue
                dramas.append(dict(versions[0], product_id=product["id"], product_name=product["name"], matched_input=matched))
            for missing in ids:
                if missing not in found_inputs:
                    reason = "产品未配置有效的短剧主产品" if product["parent_id"] in ("", "0") else "所选产品下未找到该 ID"
                    blockers.append(dict(code="drama_not_found", message=reason, product_id=product["id"], input_id=missing))
        return dramas, blockers

    def _ad_rows(self, condition, product_ids):
        return self.read("""SELECT CAST(a.id AS CHAR),a.product,a.ad_id,a.creative_id,a.video_id,
            a.source_id,a.original_source_id,a.ad_account_id,CAST(a.user_id AS CHAR),
            a.campaign_id,a.ad_name,a.campaign_name,a.status
            FROM {s}.ads_facebook_auto_created_data a WHERE a.product IN {products}
            AND a.ad_id<>'' AND ({condition}) ORDER BY a.id LIMIT 10001""".format(s=self.schema, products=inside(product_ids), condition=condition), self.AD_COLUMNS, 60)

    def resolve_ads(self, dramas, products):
        """Use indexed source lineage; exact historical markers are supplemental."""
        if not dramas:
            return [], []
        target_by_product = {}
        for drama in dramas:
            target_by_product.setdefault(drama["product_id"], {})[str(drama["content_id"])] = drama
        content_ids = sorted({str(d["content_id"]) for d in dramas})
        product_ids = [p["id"] for p in products]
        parent_names = {p["id"]: str(p.get("parent_name") or "").strip().casefold() for p in products}
        materials, source_links, raw_rows = {}, {}, {}
        for part in chunks(content_ids):
            rows = self.read("SELECT CAST(id AS CHAR),data_source_id,product,language FROM %s.ads_custom_source WHERE data_source=6 AND data_source_id IN %s LIMIT 200001" % (self.schema, inside(part)),
                ("id", "content_id", "product", "language"), 60)
            if len(rows) > 200000:
                raise AssetError("too_many_materials", "关联素材过多，请缩小范围")
            materials.update((r["id"], r) for r in rows)
        for part in chunks(materials):
            links = self.read("SELECT CAST(id AS CHAR),source_id FROM %s.ads_source WHERE source_type=3 AND source_id IN %s LIMIT 200001" % (self.schema, inside(part)), ("id", "material_id"), 60)
            if len(links) > 200000:
                raise AssetError("too_many_sources", "素材来源记录过多，请缩小范围")
            source_links.update((r["id"], r["material_id"]) for r in links)
            rows = self._ad_rows("a.original_source_id IN %s" % inside(part), product_ids)
            raw_rows.update((r["row_id"], r) for r in rows)
        for part in chunks(source_links):
            rows = self._ad_rows("a.source_id IN %s" % inside(part), product_ids)
            raw_rows.update((r["row_id"], r) for r in rows)
        # Multi-material fields also occur without historical naming markers.
        # Complete numeric-token SQL candidates are fully parsed below.
        for field, values in (("original_source_id", materials), ("source_id", source_links)):
            for part in chunks(values, 500):
                pattern = "(^|[^0-9])(" + "|".join(part) + ")([^0-9]|$)"
                rows = self._ad_rows("a.%s REGEXP %s" % (field, q(pattern)), product_ids)
                raw_rows.update((r["row_id"], r) for r in rows)
        # Product-index bounded scan, no resource/name substring is accepted.
        # Complete markers cover historical and multi-material source fields.
        for part in chunks(content_ids, 50):
            expressions = []
            for cid in part:
                expressions.append("INSTR(LOWER(a.ad_name),LOWER(%s))>0" % q("contentid[%s]" % cid))
                expressions.append("INSTR(a.campaign_name,%s)>0" % q("$%s@" % cid))
            rows = self._ad_rows(" OR ".join(expressions), product_ids)
            raw_rows.update((r["row_id"], r) for r in rows)
        if len(raw_rows) > 10000:
            raise AssetError("too_many_ads", "命中超过 10,000 条 Ad 记录，请缩小范围")
        # Resolve every material of a candidate, including out-of-scope parts.
        unknown_source = {sid for r in raw_rows.values() for sid in stored_ids(r["source_ids_raw"]) if sid not in source_links}
        for part in chunks(unknown_source):
            rows = self.read("SELECT CAST(id AS CHAR),source_id FROM %s.ads_source WHERE source_type=3 AND id IN %s" % (self.schema, inside(part)), ("id", "material_id"))
            source_links.update((r["id"], r["material_id"]) for r in rows)
        all_materials = {x for r in raw_rows.values() for x in stored_ids(r["original_ids_raw"])} | set(source_links.values())
        for part in chunks(all_materials - set(materials)):
            rows = self.read("SELECT CAST(id AS CHAR),data_source_id,product,language FROM %s.ads_custom_source WHERE data_source=6 AND id IN %s" % (self.schema, inside(part)), ("id", "content_id", "product", "language"))
            materials.update((r["id"], r) for r in rows)
        ads, blockers = {}, []
        for row in raw_rows.values():
            targets = target_by_product.get(row["product_id"], {})
            sids, oids = stored_ids(row["source_ids_raw"]), stored_ids(row["original_ids_raw"])
            mids = set(oids) | {source_links[s] for s in sids if s in source_links}
            source_content = {str(materials[mid]["content_id"]) for mid in mids if mid in materials and materials[mid]["content_id"]}
            marked_content = content_markers(row["ad_name"], row["campaign_name"])
            evidence = source_content | marked_content
            matched = evidence & set(targets)
            if not matched:
                continue
            reason = ""
            if evidence - set(targets):
                reason = "广告包含本次范围外的剧 ID，禁止扩大删除范围"
            elif source_content and marked_content and source_content != marked_content:
                reason = "素材关联与广告中的完整剧 ID 标记冲突"
            elif any(not stored_ids_complete(row[k]) for k in ("source_ids_raw", "original_ids_raw", "video_ids_raw")):
                reason = "广告素材或视频字段无法完整解析"
            elif sids and any(sid not in source_links for sid in sids):
                reason = "广告包含无法核实的素材来源"
            elif mids and any(mid not in materials for mid in mids):
                reason = "广告包含无法核实的剧资料"
            elif any(str(materials[mid].get("product") or "").strip().casefold() != parent_names.get(row["product_id"], "") for mid in mids):
                reason = "素材所属主产品与所选投放产品不一致"
            elif any(str(materials[mid].get("language") or "").strip().casefold() != str(targets[str(materials[mid]["content_id"])]["language"]).strip().casefold() for mid in mids if str(materials[mid]["content_id"]) in targets):
                reason = "素材语言与剧 ID 的语言版本不一致"
            try:
                row["account_id"] = account_id(row["account_id"])
            except AssetError:
                reason = "广告账户 ID 无效"
            row["video_ids"] = stored_ids(row["video_ids_raw"])
            row["content_ids"] = sorted(matched)
            row["dramas"] = [targets[cid] for cid in sorted(matched)]
            row["reason"] = reason
            if row["ad_id"] in ads:
                old = ads[row["ad_id"]]
                if any(old.get(k) != row.get(k) for k in ("product_id", "account_id", "creative_id", "content_ids", "video_ids")):
                    old["reason"] = "同一 Ad 的产品或素材关联记录冲突"
                continue
            ads[row["ad_id"]] = row
        # The same Meta Ad must not be assigned to another product/account in
        # the source ledger, even when only one of those products was selected.
        for part in chunks(ads):
            rows = self.read("SELECT ad_id,product,ad_account_id FROM %s.ads_facebook_auto_created_data WHERE ad_id IN %s LIMIT 50001" % (self.schema, inside(part)), ("ad_id", "product_id", "account_id"), 60)
            if len(rows) > 50000:
                raise AssetError("ad_identity_check_incomplete", "Ad 归属记录过多，无法完成核验")
            for row in rows:
                ad = ads.get(str(row["ad_id"]))
                if ad and (str(row["product_id"]) != ad["product_id"] or str(row["account_id"]).removeprefix("act_") != ad["account_id"]):
                    ad["reason"] = "同一 Ad 在源记录中属于其他产品或广告账户"
        return list(ads.values()), blockers

    def shared_references(self, objects):
        """Fail closed if the global source reference check cannot complete."""
        refs = []
        by_kind = {kind: sorted({o["object_id"] for o in objects if o["kind"] == kind}) for kind in ("creative", "video")}
        for kind, ids in by_kind.items():
            for part in chunks(ids, 250):
                if kind == "creative":
                    condition = "creative_id IN %s" % inside(part)
                else:
                    # video_id can be a scalar, CSV, or JSON array. Match complete
                    # numeric tokens, then verify parsed IDs again in Python.
                    regex = "(^|[^0-9])(" + "|".join(part) + ")([^0-9]|$)"
                    condition = "video_id REGEXP %s" % q(regex)
                rows = self.read("SELECT ad_id,product,ad_account_id,creative_id,video_id FROM %s.ads_facebook_auto_created_data WHERE (status IS NULL OR status<>'DELETED') AND (%s) LIMIT 100001" % (self.schema, condition),
                    ("ad_id", "product_id", "account_id", "creative_id", "video_ids_raw"), 60)
                if len(rows) > 100000:
                    raise AssetError("reference_check_incomplete", "共享引用数量超出核验上限，Creative / Video 暂不能删除")
                for row in rows:
                    if kind == "video" and not stored_ids_complete(row["video_ids_raw"]):
                        raise AssetError("reference_check_incomplete", "共享视频引用记录无法完整解析，不能排除范围外引用")
                    found = {row["creative_id"]} if kind == "creative" else set(stored_ids(row["video_ids_raw"]))
                    for oid in found & set(part):
                        refs.append(dict(row, key=kind + ":" + oid))
        return refs

    def token(self, user_ids):
        candidates = [str(x) for x in user_ids if re.fullmatch(r"[1-9][0-9]*", str(x))]
        if not candidates:
            return ""
        rows = self.read("SELECT CAST(user_id AS CHAR),accessToken FROM %s.ads_facebook_info WHERE user_id IN %s AND accessToken<>''" % (self.schema, inside(candidates)), ("user_id", "token"))
        by_id = {r["user_id"]: r["token"] for r in rows}
        return next((by_id[x] for x in candidates if by_id.get(x)), "")
