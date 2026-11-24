"""Immutable editorial plans shared by rough previews and render operations."""

import hashlib
import json
import time

from .runtime import check, ident, owner, packed


def setup(rt):
    with rt.db(True) as db:
        db.execute(
            "CREATE INDEX IF NOT EXISTS catalog_imports_ready_asset ON catalog_imports(owner,asset_id) WHERE status='ready'"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS studio_story_defaults(singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        db.execute("INSERT OR IGNORE INTO studio_story_defaults VALUES(1,1)")
        cols = {r[1] for r in db.execute("PRAGMA table_info(studio_sessions)")}
        if "edit_version" not in cols:
            db.execute("ALTER TABLE studio_sessions ADD COLUMN edit_version INTEGER NOT NULL DEFAULT 1")
        db.execute("""CREATE TABLE IF NOT EXISTS studio_story_plans(
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,session TEXT NOT NULL,signature TEXT NOT NULL,
            revision INTEGER NOT NULL,selection_revision INTEGER NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,
            UNIQUE(session,signature),UNIQUE(session,revision))""")
        db.execute("""CREATE TABLE IF NOT EXISTS studio_story_cache(
            owner TEXT NOT NULL,kind TEXT NOT NULL,signature TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,
            PRIMARY KEY(owner,kind,signature))""")


def provenance(db, who, ids):
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    results = {}
    for row in db.execute(
        "SELECT DISTINCT asset_id,product,source_key,episode FROM catalog_imports "
        "WHERE owner=? AND status='ready' AND asset_id IN (" + marks + ")",
        [who, *ids],
    ):
        aid = row[0]
        results[aid] = (tuple(row[1:3]), row[3]) if aid not in results else (None, None)
    return results


def order_hint(rt, actor, item):
    selected = (item.get("selection") or {}).get("items") or [
        {"asset_id": a["id"], "asset": a} for a in item["assets"]
    ]
    with rt.db() as db:
        known = provenance(db, owner(actor), [s.get("asset_id") for s in selected if s.get("asset_id")])
    rows = []
    for s in selected:
        asset = s.get("asset") or {}
        if s.get("product") and s.get("source_key"):
            identity, episode = (s["product"], s["source_key"]), s["episode"]
        else:
            episode = asset.get("episode_number")
            identity, episode = known.get(
                s.get("asset_id"),
                (("asset", asset["series_code"]) if episode and asset.get("series_code") else None, episode),
            )
        rows.append({"identity": identity, "episode": episode, "title": s.get("name") or asset.get("name", "")})
    from .story_planner import ordered

    ordered_rows, mode = ordered(rows, "episode")
    label = lambda r: ("第 %s 集" % r["episode"]) if r["episode"] else r["title"]
    return {
        "selection": [label(r) for r in rows],
        "episode": [label(r) for r in ordered_rows],
        "verified": mode == "episode",
    }


def session_config(value, version):
    from .studio import configuration

    check(isinstance(value, dict), "config_invalid", "制作配置无效")
    clip = value.get("clip")
    if version == 2 and isinstance(clip, dict) and clip.get("method", "cliffhanger") == "cliffhanger":
        value = {**value, "clip": {"method": "cliffhanger", "strategy_version": 2, **clip}}
    return configuration(value)


def initial(db, value):
    version = db.execute("SELECT version FROM studio_story_defaults WHERE singleton=1").fetchone()[0]
    check(isinstance(value, dict), "config_invalid", "制作配置无效")
    value = dict(value)
    if version == 2 and not any(value.get(k) for k in ("clip", "translation", "narration", "youtube_regular")):
        value["clip"] = {"method": "cliffhanger", "strategy_version": 2, "effects": ["basic"]}
    return version, session_config(value, version)


def is_story(config):
    return config.get("clip", {}).get("strategy_version") == 2


def signature(config, selection_id, revision, source_ids):
    if not is_story(config):
        return ""
    c = config["clip"]
    return hashlib.sha256(
        packed(
            [
                selection_id,
                revision,
                [] if selection_id else source_ids,
                config.get("merge", True),
                [
                    c[k]
                    for k in (
                        "strategy_version",
                        "story_order",
                        "seconds",
                        "script_count",
                        "variant_count",
                        "opening_take",
                        "ending_take",
                    )
                ],
            ]
        ).encode()
    ).hexdigest()


def cached(rt, actor, kind, key):
    with rt.db() as db:
        row = db.execute(
            "SELECT body,created FROM studio_story_cache WHERE owner=? AND kind=? AND signature=?",
            (owner(actor), kind, key),
        ).fetchone()
    if row and (row["body"] != "{}" or row["created"] > time.time() - 120):
        return json.loads(row["body"])
    return None


def remember(rt, actor, kind, key, data):
    with rt.db(True) as db:
        db.execute(
            "INSERT OR REPLACE INTO studio_story_cache VALUES(?,?,?,?,?)",
            (owner(actor), kind, key, packed(data), time.time()),
        )


def header(db, who, sid, key):
    if not key:
        return None
    row = db.execute(
        "SELECT id,revision,selection_revision FROM studio_story_plans WHERE owner=? AND session=? AND signature=?",
        (who, sid, key),
    ).fetchone()
    return dict(row) if row else None


def read(rt, actor, sid, pid, internal=False):
    with rt.db() as db:
        row = db.execute(
            "SELECT * FROM studio_story_plans WHERE owner=? AND session=? AND id=?", (owner(actor), sid, pid)
        ).fetchone()
    check(row, "edit_plan_missing", "此剪辑方案不存在或没有访问权限", 404)
    plan = json.loads(row["body"])
    if not internal:
        plan.pop("units", None)
        plan.pop("fingerprints", None)
    return dict(plan, id=pid, revision=row["revision"], selection_revision=row["selection_revision"])


def ensure(rt, actor, sid, frozen, analysis):
    from .story_planner import assemble, resolve_sources

    key = signature(
        frozen["config"],
        frozen.get("selection_id", ""),
        frozen.get("selection_revision", 0),
        frozen["source_ids"],
    )
    who = owner(actor)
    with rt.db() as db:
        prior = header(db, who, sid, key)
    if prior:
        plan = read(rt, actor, sid, prior["id"], True)
        check(
            all(rt.asset(aid, actor)["sha256"] == sha for aid, sha in plan["fingerprints"].items()),
            "story_source_changed",
            "剪辑方案的原片已变化，请重新确认素材",
            409,
        )
        return plan
    sources = resolve_sources(rt, actor, analysis["sources"])
    expected = frozen.get("source_ids", [])
    if frozen.get("selection_id"):
        with rt.db() as db:
            expected = [
                r[0]
                for r in db.execute(
                    "SELECT i.asset_id FROM studio_source_items i JOIN studio_source_batches b "
                    "ON b.id=i.batch WHERE b.id=? AND b.owner=? AND b.session=? ORDER BY i.position",
                    (frozen["selection_id"], who, sid),
                )
            ]
    check(
        expected == [s["source_id"] for s in sources],
        "story_selection_changed",
        "解析回执与冻结的素材选择不同",
        409,
    )
    # The public analysis receipt omits private content fingerprints.
    with rt.db() as db:
        row = db.execute(
            "SELECT sources FROM ai_edit_smart_analysis WHERE id=? AND owner=?", (analysis["analysis_id"], who)
        ).fetchone()
    check(row, "story_analysis_missing", "素材解析回执不存在", 409)
    fingerprints = {s["source_id"]: s["_stamp"] for s in json.loads(row[0])}
    check(
        all(fingerprints.get(s["source_id"]) == s["asset"]["sha256"] for s in sources),
        "story_source_changed",
        "解析之后原片已变化，请重新解析",
        409,
    )
    plan = assemble(rt, actor, sources, frozen["config"], analysis["analysis_id"])
    plan["fingerprints"] = fingerprints
    with rt.db(True) as db:
        prior = header(db, who, sid, key)
        if prior:
            pid = prior["id"]
        else:
            pid = ident("ep")
            revision = db.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM studio_story_plans WHERE session=?", (sid,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO studio_story_plans VALUES(?,?,?,?,?,?,?,?)",
                (pid, who, sid, key, revision, frozen.get("selection_revision", 0), packed(plan), time.time()),
            )
    return read(rt, actor, sid, pid, True)
