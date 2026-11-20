"""Source-bound ordinary YouTube editing, including immutable manual revisions."""

import json
import re
import time

from .catalog import decode_key
from .runtime import check, ident, owner, packed, probe
from .smart_plans import build_plans


def settings(value):
    check(isinstance(value, dict), "regular_config", "请指定正片和片头素材")
    result = {}
    for key in ("body_source_ids", "hook_source_ids"):
        ids = value.get(key, [])
        check(
            isinstance(ids, list) and len(ids) <= 20
            and all(isinstance(s, str) and re.fullmatch(r"aa_[0-9a-f]{32}", s) for s in ids)
            and len(ids) == len(set(ids)),
            "regular_sources", "请选择已导入的素材，每条只需选一次",
        )
        result[key] = list(ids)
    check(result["body_source_ids"], "regular_body", "请至少选择一条正片")
    return result


def check_sources(config, source_ids):
    selected = set(config["body_source_ids"]) | set(config["hook_source_ids"])
    check(selected == set(source_ids), "regular_sources_changed",
          "当前素材选择已改变，请重新设置正片和片头")


def catalog_identity(rt, actor, ids):
    # Historical imports also retain the catalog's verified key and owner.
    with rt.db() as db:
        rows = db.execute(
            "SELECT i.product,i.source_key,i.title,a.series FROM catalog_imports i JOIN assets a ON "
            "a.id=i.asset_id AND a.owner=i.owner WHERE i.owner=? AND i.status='ready' AND i.asset_id IN ("
            + ",".join("?" for _ in ids) + ")",
            (owner(actor), *ids),
        ).fetchall()
    if not rows:
        return None
    keys = {(r["product"], r["source_key"]) for r in rows}
    check(len(keys) == 1, "regular_drama_mixed", "请选择同一部剧、同一语言版本的正片和片头", 409)
    first = rows[0]
    product, key = next(iter(keys))
    values = decode_key(key, product)
    language = values[3] if product == "dramawave" else first["series"].rsplit(" · ", 1)[-1]
    return {"product": product, "source_key": key, "content_id": values[0], "language": language,
            "title": first["title"].rsplit(" · 第", 1)[0]}


def build(rt, actor, analysis, config):
    sources = {s["source_id"]: s for s in analysis["sources"]}
    check_sources(config, list(sources))
    body = config["body_source_ids"]
    hook_pool = config["hook_source_ids"] or body
    drama = catalog_identity(rt, actor, list(sources))
    # Reuse the existing evidence scorer; the extra hook clips never become full episodes.
    plan = build_plans([sources[s] for s in hook_pool], {"method": "hook_first"})
    opening = plan["drafts"][0]["units"][0]["clips"][0]
    clips = [dict(opening, id="hook")]
    for i, sid in enumerate(body, 1):
        clips.append({"id": "body_%d" % i, "source_id": sid, "start_ms": 0,
                      "end_ms": sources[sid]["duration_ms"]})
    reference = rt.asset(body[0], actor)
    meta = reference["metadata"]
    if not meta.get("display_width"):
        # Existing local and COS assets predate orientation/SAR metadata.
        meta = probe(rt.local_asset(body[0], actor)["path"])
    canvas = output_canvas(meta["display_width"], meta["display_height"])
    profile = {**config, "reference_source_id": body[0], "canvas": canvas,
               "drama": drama, "analysis_id": analysis["analysis_id"],
               "original_task_id": "", "parent_task_id": ""}
    name = drama["title"] if drama and drama["title"] else sources[body[0]]["title"]
    return [{"id": "youtube_regular", "title": name[:160] + " · YouTube 普通视频",
             "series_code": reference["series_code"], "clips": clips,
             "youtube_regular": profile}], plan["warnings"]


def output_canvas(width, height):
    # Preserve display proportions with square pixels at up to 1080p/1920 long edge.
    ratio = min(1, 1920 / max(width, height))
    return [max(2, 2 * round(side * ratio / 2)) for side in (width, height)]


def _task(rt, actor, tid):
    with rt.db() as db:
        row = db.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (tid, owner(actor))).fetchone()
    check(row is not None, "task_missing", "作品不存在或没有访问权限", 404)
    unit = json.loads(row["unit_data"])
    check(unit.get("youtube_regular"), "regular_task_required", "请选择普通视频作品", 409)
    return row, unit


def timeline(rt, actor, tid):
    row, unit = _task(rt, actor, tid)
    sources = {}
    for clip in unit["clips"]:
        sid = clip["source_id"]
        if sid not in sources:
            asset = rt.asset(sid, actor)
            sources[sid] = {k: asset[k] for k in ("name", "duration_ms", "preview_url")}
    return {"task": rt.task_info(row), "clips": unit["clips"], "sources": sources}


def refine(rt, actor, tid, body):
    row, unit = _task(rt, actor, tid)
    check(row["status"] == "completed", "regular_not_ready", "请先等待成片完成", 409)
    edits = body.get("clips")
    original = {c["id"]: c for c in unit["clips"]}
    check(isinstance(edits, list) and 0 < len(edits) <= len(original),
          "regular_refine_empty", "请至少保留一个片段")
    seen, clips = set(), []
    for edit in edits:
        cid = edit.get("id") if isinstance(edit, dict) else None
        check(isinstance(cid, str) and cid in original and cid not in seen, "regular_clip",
              "只可调整当前版本中已有的片段")
        seen.add(cid)
        clips.append(dict(original[cid], start_ms=edit.get("start_ms"), end_ms=edit.get("end_ms")))
    profile = dict(unit["youtube_regular"], parent_task_id=tid,
                   original_task_id=unit["youtube_regular"]["original_task_id"] or tid)
    next_unit = rt.normalize([dict(unit, clips=clips, youtube_regular=profile)], actor, regular=True)[0]

    def write(db, who):
        project = db.execute("SELECT version FROM projects WHERE id=? AND owner=?", (row["project"], who)).fetchone()
        check(project is not None, "project_missing", "项目不存在或没有访问权限", 404)
        version, now, task_id = project["version"] + 1, time.time(), ident("at")
        db.execute("UPDATE projects SET units=?,version=?,updated=? WHERE id=? AND owner=?",
                   (packed([next_unit]), version, now, row["project"], who))
        db.execute("INSERT INTO tasks(id,owner,project,version,unit_id,unit_data,status,created) VALUES(?,?,?,?,?,?,?,?)",
                   (task_id, who, row["project"], version, next_unit["id"], packed(next_unit), "queued", now))
        return {"item": rt.task_info(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())}

    return rt.receipt(actor, "regular-refine:" + tid, body, write)
