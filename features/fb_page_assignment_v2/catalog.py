"""Resolve publication-owned pools and source materials in one replica transaction."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import re
import time

from .snapshot import SUPPORTED_PRODUCTS, language


class SelectionError(Exception):
    def __init__(self, status, code):
        self.status, self.code = int(status), str(code)
        super().__init__(self.status, self.code)


@dataclass(frozen=True)
class Task:
    kind: str
    key: int


def task_ref(value):
    value = str(value)
    if re.fullmatch(r"[1-9]\d{0,17}", value):
        return Task("cross", int(value))
    bits = value.split(":", 2)
    if (len(bits) in (2, 3) and bits[0] in ("cross", "native")
            and re.fullmatch(r"[1-9]\d{0,17}", bits[1]) and all(bits)):
        return Task(bits[0], int(bits[1]))
    raise SelectionError(403, "publication_task_unverifiable")


def numbers(raw, *, object_keys=False):
    if raw is None:
        return set()
    if isinstance(raw, (list, dict)):
        values = raw
    else:
        text = str(raw).strip()
        if not text:
            return set()
        try:
            values = json.loads(text)
        except (ValueError, TypeError):
            values = text.split(",")
    if isinstance(values, dict):
        values = values.keys() if object_keys else values.values()
    elif not isinstance(values, (list, tuple, set)):
        values = [values]
    result = set()
    for value in values:
        if isinstance(value, str):
            # Numeric comma lists are also stored inside a JSON string.
            for token in value.split(","):
                token = token.strip()
                if re.fullmatch(r"[1-9]\d{0,19}", token):
                    result.add(int(token))
        elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
            result.add(value)
    return result


APPS = {"dramawave": 1479, "freereels": 979, "hotdrama": 3302,
        "1479": 1479, "979": 979, "3302": 3302}


class Reads:
    def __init__(self, cursor, deadline):
        self.cursor, self.deadline = cursor, deadline

    def query(self, template, args=(), *, timeout=8000):
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining < 1:
            raise SelectionError(503, "selection_deadline")
        sql = template.replace("SELECT ", "SELECT /*+ MAX_EXECUTION_TIME(%d) */ " % min(remaining, timeout), 1)
        self.cursor.execute(sql, args)
        cols = [v[0] for v in self.cursor.description]
        return [dict(zip(cols, row)) for row in self.cursor.fetchall()]

    def many(self, sql, ids, *, size=400, before=(), after=(), timeout=8000):
        ids = sorted(set(ids))
        rows = []
        for i in range(0, len(ids), size):
            batch = ids[i:i+size]
            stmt = sql.replace("{ids}", ",".join(["%s"] * len(batch)))
            rows.extend(self.query(stmt, (*before, *batch, *after), timeout=timeout))
        return rows


TABLE = "kunlunads_dev."


def permissions(reader, refs):
    """Use selected FB children when present; never infer a pool from its parent."""
    cross_ids = {v.key for v in refs if v.kind == "cross"}
    native_ids = {v.key for v in refs if v.kind == "native"}
    crosses = {int(r["id"]): r for r in reader.many(
        "SELECT id,supper_id,user_id,app_id,source_id FROM " + TABLE +
        "ads_cross_template_queue WHERE id IN ({ids})", cross_ids)}
    natives = {int(r["id"]): r for r in reader.many(
        "SELECT id,user_id,template_make_id,ad_account,target_ids FROM " + TABLE +
        "ads_template_make_queue WHERE id IN ({ids})", native_ids)}

    child_by_cross = defaultdict(list)
    child_by_native = defaultdict(list)
    fields = "SELECT super_queue_id,publish_queue_id,template_id,page_group_id FROM " + TABLE
    for row in reader.many(fields + "ads_cross_sub_template_accounts FORCE INDEX(idx_sqid) "
                           "WHERE super_queue_id IN ({ids}) AND is_selected=1 AND template_type IN (0,1)", cross_ids):
        child_by_cross[int(row["super_queue_id"])].append(row)
    for row in reader.many(fields + "ads_cross_sub_template_accounts FORCE INDEX(idx_pqid) "
                           "WHERE publish_queue_id IN ({ids}) AND is_selected=1 AND template_type IN (0,1)", native_ids):
        native = natives.get(int(row["publish_queue_id"]))
        if native and str(native["template_make_id"]) == str(row["template_id"]):
            child_by_native[int(native["id"])].append(row)
    # The prior 1841 canary has a separately approved pool. Its selected child
    # rows carry the old control pool, so resolve this exception by the current
    # auto template's owner, source product and exact super-template membership.
    parents = {int(row["super_queue_id"]) for vals in child_by_native.values() for row in vals if row["super_queue_id"]}
    for r in reader.many("SELECT id,supper_id,user_id,app_id,source_id FROM " + TABLE +
                         "ads_cross_template_queue WHERE id IN ({ids})", parents - crosses.keys()):
        crosses[int(r["id"])] = r
    canary = reader.query("SELECT user_id,app_id,template_ids FROM " + TABLE +
                          "ads_auto_publishs_queue WHERE id=1841") if crosses else []
    canary_ids = numbers(canary[0]["template_ids"]) if canary else set()

    def experiment(parent):
        return (bool(canary) and parent and str(parent["user_id"]) == str(canary[0]["user_id"])
                and str(parent["app_id"]) == str(canary[0]["app_id"]) == "1479"
                and int(parent["supper_id"]) in canary_ids)

    materials, grants = {}, {}
    for key in cross_ids:
        parent = crosses.get(key)
        if not parent:
            continue
        ref = Task("cross", key)
        materials[ref] = numbers(parent["source_id"], object_keys=True)
        if experiment(parent):
            grants[ref] = {62}
        else:
            # A parent spanning pools needs a native child ID to disambiguate.
            pools = {int(r["page_group_id"] or 0) for r in child_by_cross[key]}
            grants[ref] = pools if len(pools) == 1 and 0 not in pools else set()

    direct = {}
    for key, row in natives.items():
        ref = Task("native", key)
        materials[ref] = numbers(row["target_ids"])
        children = child_by_native[key]
        if children:
            choices = {62 if experiment(crosses.get(int(c["super_queue_id"] or 0)))
                       else int(c["page_group_id"] or 0) for c in children}
            grants[ref] = choices if len(choices) == 1 and 0 not in choices else set()
        else:
            direct[ref] = numbers(row["ad_account"])
    account_ids = {acct for ids in direct.values() for acct in ids}
    accounts = {int(r["id"]): str(r["account_id"]) for r in reader.many(
        "SELECT id,account_id FROM " + TABLE + "ads_accounts_setting WHERE id IN ({ids}) "
        "AND platform_id=0 AND account_status=1", account_ids)}
    by_account = defaultdict(set)
    for r in reader.many("SELECT a.account_id,g.page_group_id,g.page_group_id2 FROM " + TABLE +
                         "ads_alternative_accounts a FORCE INDEX(idx_account) JOIN " + TABLE +
                         "ads_alternative_account_group g ON g.id=a.group_id WHERE a.account_id IN ({ids}) "
                         "AND a.platform=0 AND a.deleted_at=0", accounts.values()):
        by_account[str(r["account_id"])].update(int(v) for v in (r["page_group_id"], r["page_group_id2"]) if v)
    for ref, ids in direct.items():
        pools = [by_account[accounts[i]] if i in accounts else set() for i in ids]
        grants[ref] = set.intersection(*pools) if pools else set()
    return grants, materials


def source_dramas(reader, allowed, tasks, inputs):
    ids = {i for t in allowed for i in tasks[t]}
    materials = {}
    for row in reader.many("SELECT s.id,c.data_source_id,c.product,c.language FROM " + TABLE +
                           "ads_source s JOIN " + TABLE +
                           "ads_custom_source c ON s.source_type=3 AND c.id=CAST(s.source_id AS UNSIGNED) "
                           "WHERE s.id IN ({ids})", ids):
        cid = str(row["data_source_id"] or "").strip()
        if cid:
            label = str(row["product"] or "").strip().casefold()
            materials[int(row["id"])] = (cid, APPS.get(label, -1) if label else None, language(row["language"]))
    wanted = {cid for t in allowed for cid in inputs[t]}
    infos = defaultdict(set)
    for row in reader.many("SELECT DISTINCT content_id,app_id,language FROM " + TABLE +
                           "ads_drama_info FORCE INDEX(ac) WHERE content_id IN ({ids}) "
                           "AND app_id IN (1479,979,3302)", wanted):
        # MySQL collation may fold characters; identities are case-sensitive.
        cid = str(row["content_id"])
        if cid in wanted:
            infos[cid].add((int(row["app_id"]), language(row["language"])))
    resolved = {}
    for ref in allowed:
        mappings = defaultdict(set)
        for sid in tasks[ref]:
            if sid in materials:
                cid, app, lang = materials[sid]
                mappings[cid].add((app, lang))
        for cid in inputs[ref]:
            hints = mappings.get(cid, set())
            candidates = set(infos.get(cid, ()))
            if tasks[ref] and cid not in mappings:
                resolved[ref, cid] = {"error": "drama_not_in_task"}
                continue
            if len(hints) > 1:
                resolved[ref, cid] = {"error": "drama_source_ambiguous"}
                continue
            if hints:
                app, lang = next(iter(hints))
                candidates = {(a, l) for a, l in candidates if (app is None or app == a) and (not lang or lang == l)}
            if len(candidates) == 1 and next(iter(candidates))[1] not in ("", "unknown", "__unknown__"):
                app, lang = next(iter(candidates))
                resolved[ref, cid] = {"app": app, "language": lang, "id": cid}
            else:
                resolved[ref, cid] = {"error": "drama_source_ambiguous" if candidates else "drama_not_found"}
    return resolved


def page_states(reader, groups):
    headers = {int(r["id"]): int(r["app_id"]) for r in reader.many(
        "SELECT id,app_id FROM " + TABLE + "ads_facebook_page_group WHERE id IN ({ids}) AND is_delete=0", groups)
        if int(r["app_id"] or 0) in SUPPORTED_PRODUCTS}
    raw = reader.many("SELECT i.group_id,i.page_id,i.language,p.id source_id,p.status,p.forbidden_status,"
                      "p.is_permission,p.active_ad_count,p.fan_ccount,p.updated_at FROM " + TABLE +
                      "ads_facebook_page_group_ins i JOIN " + TABLE +
                      "ads_facebook_page_group g ON g.id=i.group_id AND g.is_delete=0 LEFT JOIN " + TABLE +
                      "ads_facebook_page_ins p ON p.page_id=i.page_id WHERE i.group_id IN ({ids}) "
                      "AND i.deleted_at=0", headers, size=32)
    by_page = defaultdict(list)
    for r in raw:
        pid = str(r["page_id"] or "").strip()
        if re.fullmatch(r"[1-9][0-9]{0,31}", pid):
            by_page[int(r["group_id"]), pid].append(r)
    pools = {k: [] for k in headers}
    for (group, pid), rows in sorted(by_page.items()):
        row = max(rows, key=lambda r: (str(r["updated_at"] or ""), int(r["source_id"] or 0)))
        langs = {language(r["language"]) for r in rows}
        lang = next(iter(langs)) if len(langs) == 1 else ""
        status = row["status"] if len(langs) == 1 and lang not in ("unknown", "__unknown__") else 1
        pools[group].append([pid, lang, status, row["forbidden_status"], row["is_permission"],
                             row["active_ad_count"], row["fan_ccount"], str(row["updated_at"] or "")])
    return pools
