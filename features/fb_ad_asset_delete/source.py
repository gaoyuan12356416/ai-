"""Read-only business-source adapter; all queries use the existing SQL FIFO gate."""
import hashlib
import logging
import re
import time
from .core import AssetError, account_id, content_markers, stored_ids, stored_ids_complete, stored_video_ids, creative_video_ids
from .video_index import ReferenceRows


logger = logging.getLogger(__name__)


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
    AD_SCAN_COLUMNS = ("row_id", "ad_id", "source_ids_raw", "original_ids_raw", "ad_name", "campaign_name")
    AD_SCAN_PAGE_SIZE = 5000
    AD_SCAN_MIN_PAGE_SIZE = 100
    PREVIEW_READ_ATTEMPTS = 3

    def __init__(self, query, schema="kunlunads_dev", lookup_actor=None, video_index=None, reference_graph_factory=None):
        if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
            raise ValueError("invalid source schema")
        self.query = query
        self.schema = "`%s`" % schema
        self.lookup_actor = lookup_actor or (lambda session: {})
        self.video_index = video_index
        self.reference_graph_factory = reference_graph_factory

    def read(self, sql, columns, timeout=30):
        if not sql.lstrip().upper().startswith("SELECT"):
            raise AssetError("read_only_source", "业务源库仅允许读取", 500)
        statement = " ".join(sql.split())
        started = time.monotonic()
        try:
            rows = self.query(statement, timeout)
        except Exception as exc:
            error = AssetError("source_unavailable", "业务数据查询未完成，请稍后重试；此次不能执行删除", 503)
            # Retry only recognized transport/timeout failures. SQL errors and
            # permission errors must surface without repeating an invalid read.
            codes = {1040, 1205, 1213, 2002, 2003, 2006, 2013}
            code = getattr(exc, "errno", None)
            if code is None and getattr(exc, "args", ()):
                code = exc.args[0]
            stderr = getattr(exc, "stderr", "") or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            server_code = re.search(r"\bERROR\s+(\d{4})\b", str(stderr), re.I)
            if server_code:
                code = int(server_code.group(1))
            error.retryable = (isinstance(exc, (TimeoutError, ConnectionError)) or
                getattr(exc, "returncode", None) in (124, 137) or
                isinstance(code, int) and code in codes or
                bool(re.search(r"timed out|timeouterror|deadline exceeded|lost connection|server has gone away|connection (?:reset|refused)", str(exc), re.I)))
            return_code = getattr(exc, "returncode", None)
            # Raw SQL, stderr and exception text may contain tokens from other
            # source methods, so the only query identity logged is its hash.
            logger.warning("business-source read failed query_sha256=%s duration_ms=%d error_class=%s return_code=%s error_number=%s retryable=%s",
                hashlib.sha256(statement.encode("utf-8")).hexdigest()[:16],
                int((time.monotonic() - started) * 1000), type(exc).__name__,
                return_code if isinstance(return_code, int) else None,
                code if isinstance(code, int) else None, error.retryable)
            raise error from None
        return [dict(zip(columns, row)) for row in rows]

    def _preview_read(self, sql, columns, timeout=30, progress=None):
        """Bounded retries for read-only preview lookups; never for mutations."""
        for attempt in range(self.PREVIEW_READ_ATTEMPTS):
            try:
                return self.read(sql, columns, timeout)
            except AssetError as exc:
                if not getattr(exc, "retryable", False) or attempt + 1 == self.PREVIEW_READ_ATTEMPTS:
                    raise
                if progress:
                    progress(dict(stage="ads", retry_count=attempt + 1,
                        message="查询短暂中断，正在自动重试（%d/%d）" % (attempt + 1, self.PREVIEW_READ_ATTEMPTS - 1)))
                time.sleep(0.25 * (attempt + 1))

    def _products(self, session, ids=None):
        """Read current ACL/default mapping, optionally only for explicit IDs."""
        permission = "1=1"
        if session.get("role") != "admin":
            group = self.lookup_actor(session) or {}
            sub_id = str(group.get("sub_user_id") or "")
            if not sub_id.isdigit():
                return []
            permission = """EXISTS (SELECT 1 FROM {s}.admin_role_users aru
                JOIN {s}.admin_role_apps ara ON ara.id=aru.role_app_id
                WHERE aru.user_id={uid} AND (ara.is_all=1 OR FIND_IN_SET(CAST(a.id AS CHAR),REPLACE(ara.values,' ',''))))""".format(s=self.schema, uid=q(sub_id))
        selection = " AND a.id IN " + inside(ids) if ids is not None else ""
        rows = self.read("""SELECT CAST(a.id AS CHAR),a.name,CAST(a.app_type AS CHAR),
            CAST(CASE WHEN a.app_type=14 THEN a.id ELSE COALESCE(p.id,0) END AS CHAR),
            CASE WHEN a.app_type=14 THEN a.name ELSE COALESCE(p.name,'') END,
            CAST(COALESCE(a.default_user,0) AS CHAR)
            FROM {s}.ads_apps_setting a LEFT JOIN {s}.ads_apps_setting p ON p.id=a.ads_app_id AND p.app_type=14
            WHERE (a.app_type=14 OR (a.app_type IN (10000,10001) AND (a.landing_app_type=14 OR p.id IS NOT NULL)))
            AND ({permission}){selection} ORDER BY a.name,a.id LIMIT 5001""".format(
                s=self.schema, permission=permission, selection=selection),
            ("id", "name", "app_type", "parent_id", "parent_name", "default_user"))
        if len(rows) > 5000:
            raise AssetError("too_many_products", "短剧产品目录超出可读取范围", 503)
        for row in rows:
            row["kind"] = "App" if str(row.pop("app_type")) == "14" else "W2A"
            row["id"] = str(row["id"])
            row["parent_id"] = str(row["parent_id"])
        return rows

    def list_products(self, session):
        return self._products(session)

    def selected_products(self, session, ids):
        ids = list(ids)
        if not ids:
            return []
        allowed = {row["id"]: row for row in self._products(session, list(dict.fromkeys(ids)))}
        if any(pid not in allowed for pid in ids):
            raise AssetError("product_permission_denied", "所选产品不属于可访问的短剧投放产品", 403)
        return [allowed[pid] for pid in ids]

    def resolve_dramas(self, input_type, ids, products):
        parent_ids = sorted({p["parent_id"] for p in products if p["parent_id"] not in ("", "0")})
        rows = []
        if parent_ids:
            rows = self._preview_read("SELECT CAST(app_id AS CHAR),content_id,series_code,language,name FROM %s.ads_drama_info WHERE app_id IN %s AND %s IN %s ORDER BY id LIMIT 50001" %
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

    def _scan_ad_candidates(self, target_by_product, materials, source_links, progress=None):
        """Read each product once with bounded indexed keyset pages.

        Source fields and historical markers are parsed in Python instead of
        issuing one full product REGEXP/INSTR scan per material/content batch.
        The watermark freezes inserts. This is not a cross-query transactional
        snapshot: existing rows can change, just as with the former lookups;
        candidate lineage is checked again when full records are fetched and
        the service must still perform its current Meta verification.
        """
        candidates, scanned, retries = {}, 0, 0
        product_ids = sorted(target_by_product)
        total = len(product_ids)
        material_ids, source_ids = set(materials), set(source_links)
        for product_number, pid in enumerate(product_ids):
            target_ids = set(target_by_product[pid])
            bounds = self._preview_read("SELECT CAST(COALESCE(MAX(a.id),0) AS CHAR),COUNT(*) FROM %s.ads_facebook_auto_created_data a FORCE INDEX (idx_product) WHERE a.product=%s" % (self.schema, q(pid)),
                ("max_id", "row_count"), progress=progress)
            if len(bounds) != 1:
                raise AssetError("ad_scan_incomplete", "无法确认广告扫描范围，请重新预览", 503)
            try:
                high, expected = int(bounds[0]["max_id"]), int(bounds[0]["row_count"])
            except (KeyError, TypeError, ValueError):
                raise AssetError("ad_scan_incomplete", "广告扫描范围返回不完整，请重新预览", 503) from None
            if high < 0 or expected < 0 or bool(high) != bool(expected):
                raise AssetError("ad_scan_incomplete", "广告扫描范围无效，请重新预览", 503)
            cursor, product_scanned, failures = 0, 0, 0
            page_size = self.AD_SCAN_PAGE_SIZE

            def report(message):
                if progress:
                    progress(dict(stage="ads", product_id=pid, products_done=product_number,
                        products_total=total, scanned_rows=scanned, product_scanned_rows=product_scanned,
                        product_total_rows=expected, candidate_rows=len(candidates), page_size=page_size,
                        retry_count=retries, message=message))

            report("正在分批核对第 %s/%s 个产品的广告关联（共 %s 条）" % (product_number + 1, total, expected))
            while cursor < high:
                sql = """SELECT CAST(a.id AS CHAR),a.ad_id,a.source_id,a.original_source_id,a.ad_name,a.campaign_name
                    FROM {s}.ads_facebook_auto_created_data a FORCE INDEX (idx_product)
                    WHERE a.product={product} AND a.id>{cursor} AND a.id<={high}
                    ORDER BY a.id LIMIT {size}""".format(s=self.schema, product=q(pid), cursor=cursor, high=high, size=page_size)
                try:
                    rows = self.read(sql, self.AD_SCAN_COLUMNS, 30)
                except AssetError as exc:
                    if not getattr(exc, "retryable", False) or failures + 1 >= self.PREVIEW_READ_ATTEMPTS or retries >= 12:
                        raise
                    failures += 1
                    retries += 1
                    page_size = max(self.AD_SCAN_MIN_PAGE_SIZE, page_size // 2)
                    report("查询短暂中断，正在缩小分批大小并重试当前进度")
                    time.sleep(0.25 * failures)
                    continue
                failures = 0
                if not rows or len(rows) > page_size:
                    raise AssetError("ad_scan_incomplete", "广告扫描返回不完整，请重新预览；当前不能执行删除", 503)
                for row in rows:
                    try:
                        rid = int(row["row_id"])
                    except (KeyError, TypeError, ValueError):
                        raise AssetError("ad_scan_incomplete", "广告扫描记录无效，请重新预览", 503) from None
                    if set(row) != set(self.AD_SCAN_COLUMNS) or not cursor < rid <= high:
                        raise AssetError("ad_scan_incomplete", "广告扫描记录不完整或顺序异常，请重新预览", 503)
                    cursor = rid
                    if row["ad_id"] and (set(stored_ids(row["original_ids_raw"])) & material_ids or
                            set(stored_ids(row["source_ids_raw"])) & source_ids or
                            content_markers(row["ad_name"], row["campaign_name"]) & target_ids):
                        candidates[str(rid)] = dict(row, product_id=pid)
                        if len(candidates) > 10000:
                            raise AssetError("too_many_ads", "命中超过 10,000 条 Ad 记录，请缩小范围")
                product_scanned += len(rows)
                scanned += len(rows)
                if product_scanned > expected:
                    raise AssetError("ad_scan_changed", "广告来源在扫描期间发生变化，请重新预览", 503)
                report("已核对 %s/%s 个产品，扫描 %s 条广告，找到 %s 条候选记录" % (product_number, total, scanned, len(candidates)))
                # A short page does not prove completion (the SQL transport may
                # impose a page cap). Only reaching the frozen ID bound does.
            counts = self._preview_read("SELECT COUNT(*) FROM %s.ads_facebook_auto_created_data a FORCE INDEX (idx_product) WHERE a.product=%s AND a.id<=%s" % (self.schema, q(pid), high),
                ("row_count",), progress=progress)
            if (len(counts) != 1 or str(counts[0].get("row_count")) != str(expected) or product_scanned != expected):
                raise AssetError("ad_scan_changed", "广告来源在扫描期间发生变化或返回不完整，请重新预览", 503)
            if progress:
                progress(dict(stage="ads", product_id=pid, products_done=product_number + 1,
                    products_total=total, scanned_rows=scanned, candidate_rows=len(candidates),
                    retry_count=retries, message="第 %s/%s 个产品的广告关联扫描完成" % (product_number + 1, total)))
        raw_rows = {}
        for part in chunks(candidates):
            rows = self._preview_read("""SELECT CAST(a.id AS CHAR),a.product,a.ad_id,a.creative_id,a.video_id,
                a.source_id,a.original_source_id,a.ad_account_id,CAST(a.user_id AS CHAR),
                a.campaign_id,a.ad_name,a.campaign_name,a.status
                FROM {s}.ads_facebook_auto_created_data a WHERE a.id IN {ids}
                ORDER BY a.id""".format(s=self.schema, ids=inside(part)), self.AD_COLUMNS, progress=progress)
            if len(rows) != len(part):
                raise AssetError("ad_scan_changed", "候选广告来源发生变化或返回不完整，请重新预览", 503)
            for row in rows:
                frozen = candidates.get(str(row.get("row_id")))
                if (not frozen or row["row_id"] in raw_rows or set(row) != set(self.AD_COLUMNS) or
                        any(str(row.get(key) or "") != str(value or "") for key, value in frozen.items())):
                    raise AssetError("ad_scan_changed", "候选广告的产品或素材关联发生变化，请重新预览", 503)
                raw_rows[row["row_id"]] = row
        return raw_rows

    def resolve_ads(self, dramas, products, progress=None):
        """Resolve complete lineage without repeated non-indexable SQL scans."""
        if not dramas:
            return [], []
        target_by_product = {}
        for drama in dramas:
            target_by_product.setdefault(drama["product_id"], {})[str(drama["content_id"])] = drama
        content_ids = sorted({str(d["content_id"]) for d in dramas})
        parent_names = {p["id"]: str(p.get("parent_name") or "").strip().casefold() for p in products}
        materials, source_links, raw_rows = {}, {}, {}
        if progress:
            progress(dict(stage="ads", products_done=0, products_total=len(target_by_product), scanned_rows=0,
                candidate_rows=0, message="正在读取剧目对应的素材与来源索引"))
        for part in chunks(content_ids):
            rows = self._preview_read("SELECT CAST(id AS CHAR),data_source_id,product,language FROM %s.ads_custom_source WHERE data_source=6 AND data_source_id IN %s LIMIT 200001" % (self.schema, inside(part)),
                ("id", "content_id", "product", "language"), 60, progress=progress)
            if len(rows) > 200000:
                raise AssetError("too_many_materials", "关联素材过多，请缩小范围")
            materials.update((r["id"], r) for r in rows)
        for part in chunks(materials):
            links = self._preview_read("SELECT CAST(id AS CHAR),source_id FROM %s.ads_source WHERE source_type=3 AND source_id IN %s LIMIT 200001" % (self.schema, inside(part)), ("id", "material_id"), 60, progress=progress)
            if len(links) > 200000:
                raise AssetError("too_many_sources", "素材来源记录过多，请缩小范围")
            source_links.update((r["id"], r["material_id"]) for r in links)
        raw_rows = self._scan_ad_candidates(target_by_product, materials, source_links, progress=progress)
        if progress:
            progress(dict(stage="ads", candidate_rows=len(raw_rows), message="广告扫描完成，正在核实候选记录的完整素材关系"))
        # Resolve every material of a candidate, including out-of-scope parts.
        unknown_source = {sid for r in raw_rows.values() for sid in stored_ids(r["source_ids_raw"]) if sid not in source_links}
        for part in chunks(unknown_source):
            rows = self._preview_read("SELECT CAST(id AS CHAR),source_id FROM %s.ads_source WHERE source_type=3 AND id IN %s" % (self.schema, inside(part)), ("id", "material_id"), progress=progress)
            source_links.update((r["id"], r["material_id"]) for r in rows)
        all_materials = {x for r in raw_rows.values() for x in stored_ids(r["original_ids_raw"])} | set(source_links.values())
        for part in chunks(all_materials - set(materials)):
            rows = self._preview_read("SELECT CAST(id AS CHAR),data_source_id,product,language FROM %s.ads_custom_source WHERE data_source=6 AND id IN %s" % (self.schema, inside(part)), ("id", "content_id", "product", "language"), progress=progress)
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
            row["video_ids"] = stored_video_ids(row["video_ids_raw"])
            row["content_ids"] = sorted(matched)
            row["dramas"] = [targets[cid] for cid in sorted(matched)]
            row["reason"] = reason
            if row["ad_id"] in ads:
                old = ads[row["ad_id"]]
                if any(old.get(k) != row.get(k) for k in ("product_id", "account_id", "creative_id", "content_ids", "video_ids")):
                    old["reason"] = "同一 Ad 的产品或素材关联记录冲突"
                elif row["reason"] and not old["reason"]:
                    # Keyset order must never hide a blocker found in another
                    # ledger row describing the same Meta Ad.
                    old["reason"] = row["reason"]
                continue
            ads[row["ad_id"]] = row
        # The same Meta Ad must not be assigned to another product/account in
        # the source ledger, even when only one of those products was selected.
        for part in chunks(ads):
            rows = self._preview_read("SELECT ad_id,product,ad_account_id FROM %s.ads_facebook_auto_created_data WHERE ad_id IN %s LIMIT 50001" % (self.schema, inside(part)), ("ad_id", "product_id", "account_id"), 60, progress=progress)
            if len(rows) > 50000:
                raise AssetError("ad_identity_check_incomplete", "Ad 归属记录过多，无法完成核验")
            for row in rows:
                ad = ads.get(str(row["ad_id"]))
                if ad and (str(row["product_id"]) != ad["product_id"] or str(row["account_id"]).removeprefix("act_") != ad["account_id"]):
                    ad["reason"] = "同一 Ad 在源记录中属于其他产品或广告账户"
        return list(ads.values()), blockers

    def shared_references(self, objects, progress=None, fresh=False):
        """Creative account index; complete global Video snapshot, never SQL regex."""
        refs, seen = ReferenceRows(), set()
        by_kind = {kind: sorted({o["object_id"] for o in objects if o["kind"] == kind}) for kind in ("creative", "video")}
        for kind, ids in by_kind.items():
            if not ids:
                continue
            if kind == "video":
                if self.video_index is None:
                    raise AssetError("video_index_unavailable", "视频引用索引未配置，保持阻止", 503)
                resolver = (lambda rows: self.resolve_video_anomalies(rows, objects)) if self.reference_graph_factory else None
                video_refs = self.video_index.references(ids, fresh=fresh, progress=progress, resolver=resolver)
                refs.extend(video_refs)
                refs.proof = video_refs.proof
                continue
            relevant = [o for o in objects if o["kind"] == kind]
            verified = kind == "creative" and all(o.get("account_verified") and len(o.get("account_ids", [])) == 1 for o in relevant)
            condition = "creative_id IN %s" % inside(ids)
            if verified:
                accounts = {a for o in relevant for a in o["account_ids"]}
                condition += " AND ad_account_id IN %s" % inside(sorted(accounts | {"act_" + a for a in accounts}))
            index = "ad_account_id" if verified else "PRIMARY"
            if progress:
                progress(kind, 0, 1)
            rows = self.read("SELECT ad_id,product,ad_account_id,creative_id,video_id FROM %s.ads_facebook_auto_created_data FORCE INDEX (%s) WHERE (status IS NULL OR status<>'DELETED') AND (%s) LIMIT 100001" % (self.schema, index, condition),
                ("ad_id", "product_id", "account_id", "creative_id", "video_ids_raw"), 60 if verified else 180)
            if len(rows) > 100000:
                raise AssetError("reference_check_incomplete", "共享引用数量超出核验上限，相关素材暂不能删除")
            for row in rows:
                if kind == "video" and not stored_ids_complete(row["video_ids_raw"]):
                    raise AssetError("reference_check_incomplete", "共享视频引用记录无法完整解析，不能排除范围外引用")
                found = {row["creative_id"]} if kind == "creative" else set(stored_ids(row["video_ids_raw"]))
                for oid in found & set(ids):
                    key = (kind + ":" + oid, str(row["ad_id"]), str(row["product_id"]), str(row["account_id"]))
                    if key not in seen:
                        seen.add(key)
                        refs.append(dict(row, key=key[0]))
            if progress:
                progress(kind, 1, 1)
        return refs

    def validate_reference_snapshot(self, refs):
        if self.video_index is None:
            raise AssetError("video_index_unavailable", "视频引用索引未配置，保持阻止", 503)
        self.video_index.validate(getattr(refs, "proof", None))

    def resolve_video_anomalies(self, records, objects):
        """Only current Meta GET proof may repair ambiguous/truncated source fields."""
        from .graph import AD_FIELDS
        graph = self.reference_graph_factory()
        ids = [str(r["row_id"]) for r in records]
        rows = self.read("SELECT CAST(a.id AS CHAR),a.ad_id,a.ad_account_id,CAST(a.user_id AS CHAR),CAST(COALESCE(p.default_user,0) AS CHAR) FROM %s.ads_facebook_auto_created_data a LEFT JOIN %s.ads_apps_setting p ON p.id=a.product WHERE a.id IN %s LIMIT 101" % (self.schema, self.schema, inside(ids)),
                         ("row_id", "ad_id", "account_id", "user_id", "default_user"))
        source = {r["row_id"]: r for r in rows}
        fallback_users = sorted({u for o in objects for u in o.get("user_ids", [])})
        refs = []
        for record in records:
            row = source.get(str(record["row_id"]))
            aid = str(record["account_id"]).removeprefix("act_")
            try:
                if not row or row["ad_id"] != record["ad_id"] or account_id(row["account_id"]) != aid:
                    raise AssetError("reference_identity_changed", "历史引用归属发生变化")
                obj = dict(object_id=record["ad_id"], account_ids=[aid],
                           user_ids=list(dict.fromkeys([row["user_id"], row["default_user"]] + fallback_users)))
                ad = graph.read_node(obj, "id,account_id,status,effective_status")
                if account_id(ad.get("account_id")) != aid:
                    raise AssetError("account_mismatch", "历史广告所属账户无法确认")
                if ad.get("status") == "DELETED" or ad.get("effective_status") == "DELETED":
                    continue
                ad = graph.read_node(obj, AD_FIELDS.replace("asset_feed_spec", "asset_feed_spec,image_hash"))
                creative = ad.get("creative") or {}
                videos = creative_video_ids(creative)
                images = creative.get("image_hash") or (creative.get("asset_feed_spec") or {}).get("images")
                if not creative.get("id") or not videos and not images:
                    raise AssetError("reference_creative_unverified", "历史广告完整视频关系无法确认")
                refs.extend(dict(key="video:"+vid, ad_id=record["ad_id"], product_id=record["product_id"], account_id=aid) for vid in videos)
            except AssetError as exc:
                error = AssetError("video_reference_unverified", "历史视频引用字段可能截断，无法核实账户 %s 的 Ad %s。请由有权访问该账户的管理员恢复读取后重新核验（Meta/读取错误 %s）" % (aid, record["ad_id"], exc.code), 409)
                error.detail = dict(source_row_id=record["row_id"], reference_account_id=aid, reference_ad_id=record["ad_id"],
                                    cause=getattr(exc, "detail", {}), unresolved_records=len(records))
                raise error from None
        return refs

    def token(self, user_ids):
        candidates = [str(x) for x in user_ids if re.fullmatch(r"[1-9][0-9]*", str(x))]
        if not candidates:
            return ""
        rows = self.read("SELECT CAST(user_id AS CHAR),accessToken FROM %s.ads_facebook_info WHERE user_id IN %s AND accessToken<>''" % (self.schema, inside(candidates)), ("user_id", "token"))
        by_id = {r["user_id"]: r["token"] for r in rows}
        return next((by_id[x] for x in candidates if by_id.get(x)), "")

    def video_account_credential(self, obj, target_account):
        """Resolve the publishing queue's credential rule with fresh SQL reads.

        Frozen users describe source lineage, not necessarily the token owner.
        A current product default may change after preview. Only an exact source
        ad / queue / product link can authorize that default, never a flat list.
        """
        aid = account_id(target_account)
        if obj.get("kind") != "video" or aid not in {account_id(x) for x in obj.get("account_ids", [])}:
            raise AssetError("account_outside_preview", "账户不在该 Video 的冻结范围内", 409)
        valid_id = lambda value: re.fullmatch(r"[1-9][0-9]{0,31}", str(value or "")) is not None
        users = {str(x) for x in obj.get("user_ids", []) if valid_id(x)}
        ads = {str(x) for x in obj.get("ad_ids", []) if valid_id(x)}
        products = {str(x) for x in obj.get("product_ids", []) if valid_id(x)}
        if not users or not ads or not products:
            raise AssetError("credential_source_missing", "冻结记录缺少发布来源，无法确定应使用的 Token", 409)

        def scoped(row):
            return (isinstance(row, dict) and valid_id(row.get("source_row_id")) and
                str(row.get("account_id", "")).removeprefix("act_") == aid and
                str(row.get("user_id")) in users and str(row.get("ad_id")) in ads and
                str(row.get("product_id")) in products)

        frozen = obj.get("video_account_sources")
        frozen_rows = {str(r["source_row_id"]): r for r in frozen if scoped(r)} if isinstance(frozen, list) else {}
        if isinstance(frozen, list) and not frozen_rows:
            raise AssetError("credential_source_missing", "没有该广告账户的冻结发布来源", 409)
        row_filter = ""
        if frozen_rows:
            # Match each complete frozen tuple before selecting the first row;
            # independent IN lists could accept a newly crossed association.
            tuples = ["(" + ",".join(q(frozen_rows[rid][key]) for key in
                ("source_row_id", "ad_id", "product_id", "user_id")) + ")" for rid in sorted(frozen_rows)]
            row_filter = " AND a.id IN %s AND (a.id,a.ad_id,a.product,a.user_id) IN (%s)" % (
                inside(sorted(frozen_rows)), ",".join(tuples))
        columns = ("source_row_id", "product_id", "ad_id", "account_id", "user_id", "publish_queue_id",
            "queue_id", "queue_product_id", "queue_user_id", "default_token", "setting_product_id", "default_user")
        rows = self.read("""SELECT chosen.*,CAST(f.user_id AS CHAR),CAST(f.facebookUserID AS CHAR),f.accessToken
            FROM (SELECT CAST(a.id AS CHAR) AS source_row_id,a.product AS product_id,a.ad_id,
                a.ad_account_id AS account_id,CAST(a.user_id AS CHAR) AS user_id,
                CAST(a.publish_queue_id AS CHAR) AS publish_queue_id,CAST(pq.id AS CHAR) AS queue_id,
                pq.product AS queue_product_id,CAST(pq.user_id AS CHAR) AS queue_user_id,
                CAST(pq.default_token AS CHAR) AS default_token,CAST(p.id AS CHAR) AS setting_product_id,
                CAST(p.default_user AS CHAR) AS default_user
                FROM {s}.ads_facebook_auto_created_data a
                LEFT JOIN {s}.ads_template_make_queue pq ON pq.id=a.publish_queue_id
                LEFT JOIN {s}.ads_apps_setting p ON p.id=a.product
                WHERE a.ad_id IN {ads} AND a.product IN {products} AND a.user_id IN {users}
                    AND a.ad_account_id IN {accounts}{row_filter}
                ORDER BY a.id LIMIT 1) chosen
            LEFT JOIN {s}.ads_facebook_info f ON f.user_id=CASE
                WHEN chosen.default_token='1' THEN chosen.default_user
                WHEN chosen.default_token='-1' THEN chosen.queue_user_id ELSE NULL END
                AND CAST(chosen.queue_id AS BINARY)=CAST(chosen.publish_queue_id AS BINARY)
                AND CAST(chosen.queue_product_id AS BINARY)=CAST(chosen.product_id AS BINARY)
                AND CAST(chosen.queue_user_id AS BINARY)=CAST(chosen.user_id AS BINARY)
                AND TRIM(f.accessToken)<>''
            LIMIT 2""".format(s=self.schema, ads=inside(sorted(ads)), products=inside(sorted(products)),
                users=inside(sorted(users)), accounts=inside([aid, "act_" + aid]), row_filter=row_filter),
            columns + ("token_user_id", "fb_user_id", "token"), 5)
        # SQL returns one chosen source and at most two nonempty credentials.
        # This replaces the old 10,001-source scan without relaxing lineage,
        # queue diagnostics or duplicate-credential rejection.
        if len(rows) > 2:
            raise AssetError("credential_source_overflow", "发布来源查询返回超出上限，无法确定 Token", 409)
        matched = [row for row in rows if scoped(row) and (not frozen_rows or
            str(row["source_row_id"]) in frozen_rows and all(str(row[key]) == str(frozen_rows[str(row["source_row_id"])][key])
                for key in ("ad_id", "product_id", "user_id")))]
        if not matched:
            raise AssetError("credential_source_missing", "无法从冻结广告找到匹配的发布来源", 409)
        # Deterministic first source; never try a second identity after failure.
        row = min(matched, key=lambda item: int(item["source_row_id"]))
        context = dict(credential_source_row_id=str(row["source_row_id"]), credential_ad_id=str(row["ad_id"]),
            credential_product_id=str(row["product_id"]), credential_source_user_id=str(row["user_id"]))

        def fail(code, message):
            error = AssetError(code, message, 409)
            error.detail = dict(context)
            raise error

        queue_id = str(row.get("queue_id") or "")
        if not valid_id(queue_id) or queue_id != str(row.get("publish_queue_id")):
            fail("publish_queue_missing", "源广告的发布队列不存在，无法确定 Token；未改用其他用户")
        context["credential_publish_queue_id"] = queue_id
        if str(row.get("queue_product_id")) != str(row["product_id"]) or str(row.get("queue_user_id")) != str(row["user_id"]):
            fail("publish_queue_mismatch", "发布队列的产品或用户与源广告不一致，无法确定 Token")
        flag = str(row.get("default_token"))
        if flag not in ("1", "-1"):
            fail("publish_token_rule_unknown", "发布队列的默认 Token 标记无法确定（需 1 或 -1）；未改用其他用户")
        context["credential_default_token"] = flag
        if flag == "1":
            if str(row.get("setting_product_id")) != str(row["product_id"]) or not valid_id(row.get("default_user")):
                fail("product_default_token_missing", "发布队列要求使用默认 Token，但产品未配置有效默认 Token 用户")
            uid, relation = str(row["default_user"]), "product_default_user"
        else:
            uid, relation = str(row["queue_user_id"]), "publish_queue_user"
        context.update(credential_kind="user", credential_user_id=uid, credential_relation=relation)
        # The second joined row must describe the very same source. Never let
        # a malformed transport result hide a duplicate or switch identities.
        matches = [r for r in matched if str(r.get("token_user_id")) == uid and str(r.get("token") or "").strip()]
        if any(any(str(other.get(key)) != str(row.get(key)) for key in columns) for other in rows):
            fail("credential_source_changed", "发布来源查询返回不一致，无法确定 Token；未改用其他用户")
        if len(matches) != 1:
            fail("publishing_token_unavailable", "按发布队列选定的 Token 不可用或不唯一；未改用其他用户")
        token_row = matches[0]
        if valid_id(token_row.get("fb_user_id")):
            context["credential_fb_user_id"] = str(token_row["fb_user_id"])
        return dict(context, token=str(token_row["token"]).strip())

    def video_credential(self, user_ids, identity_id, relation):
        """Read one current credential for an exact identity and frozen users.

        facebookUserID/fb_user_id are Meta identities, whereas user_id is the
        internal candidate ID. No Page pool membership or product expansion is
        used here. The caller keeps its User Token when this optional read fails.
        """
        candidates = list(dict.fromkeys(str(x) for x in user_ids if re.fullmatch(r"[1-9][0-9]{0,31}", str(x))))
        identity = str(identity_id or "")
        if not candidates or not re.fullmatch(r"[1-9][0-9]{0,31}", identity) or relation not in ("video_from", "creative_page"):
            return None
        order = ",".join(q(x) for x in candidates)
        rows = self.read("""SELECT CAST(f.user_id AS CHAR),CAST(f.facebookUserID AS CHAR),
            CAST(p.id AS CHAR),CAST(p.page_id AS CHAR),CAST(p.fb_user_id AS CHAR),p.page_access_token
            FROM {s}.ads_facebook_page_post p JOIN {s}.ads_facebook_info f
              ON CAST(p.fb_user_id AS BINARY)=CAST(f.facebookUserID AS BINARY)
            WHERE f.user_id IN {users} AND p.page_id={identity}
              AND p.status<>1 AND TRIM(p.page_access_token)<>''
            ORDER BY FIELD(CAST(f.user_id AS CHAR),{order}),p.id LIMIT 101""".format(
                s=self.schema, users=inside(candidates), identity=q(identity), order=order),
            ("user_id", "fb_user_id", "row_id", "page_id", "page_fb_user_id", "token"), 5)
        pages = []
        for row in rows:
            uid, fbid, row_id = (str(row.get(k) or "") for k in ("user_id", "fb_user_id", "row_id"))
            token = str(row.get("token") or "").strip()
            if (uid in candidates and str(row.get("page_id")) == identity and
                    fbid == str(row.get("page_fb_user_id")) and
                    re.fullmatch(r"[1-9][0-9]{0,31}", fbid) and
                    re.fullmatch(r"[1-9][0-9]{0,31}", row_id) and token):
                pages.append(dict(token=token, credential_kind="page", credential_page_id=identity,
                    credential_row_id=row_id, credential_fb_user_id=fbid, credential_user_id=uid))
        if pages:
            return min(pages, key=lambda item: (candidates.index(item["credential_user_id"]), int(item["credential_row_id"])))
        if relation != "video_from":
            return None
        # A Video.from person can select a different *frozen* internal user.
        # A Creative's Page association must never select a same-numbered user.
        rows = self.read("""SELECT CAST(user_id AS CHAR),CAST(facebookUserID AS CHAR),accessToken
            FROM {s}.ads_facebook_info WHERE user_id IN {users} AND facebookUserID={identity}
              AND TRIM(accessToken)<>'' ORDER BY FIELD(CAST(user_id AS CHAR),{order}) LIMIT 101""".format(
                s=self.schema, users=inside(candidates), identity=q(identity), order=order),
            ("user_id", "fb_user_id", "token"), 5)
        users = [dict(token=str(row.get("token") or "").strip(), credential_kind="user",
                      credential_user_id=str(row.get("user_id") or ""), credential_fb_user_id=identity)
                 for row in rows if str(row.get("user_id")) in candidates and str(row.get("fb_user_id")) == identity
                 and str(row.get("token") or "").strip()]
        return min(users, key=lambda item: candidates.index(item["credential_user_id"])) if users else None
