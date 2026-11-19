"""Index existing finished videos from their original native preparation claims.

Run with the coordinator interpreter. Reads native ledgers in read-only mode.
No native job, publication, worker lease or platform receipt is modified.
"""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch import media_decision as media


def native_db(paths, channel, family, pool, source_id):
    name = "tt_auto" if (channel, family) == ("tt", "auto") else channel
    if channel == "x" and str(source_id).isdigit() and int(source_id) >= 9000000000000:
        name = "x_tag"
    if name not in paths or channel not in {"fb", "x", "tt"}:
        return None
    if name not in pool:
        path = Path(paths[name]).resolve()
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        pool[name] = db
    return pool[name]


def original_context(job, db, artifact):
    channel = job["channel"]
    table = {
        "fb": "pipeline_fb_claim",
        "x": "pipeline_x_claim",
        "tt": "pipeline_tt_claim",
    }[channel]
    if not db.execute(
        "select 1 from sqlite_master where name=? and type='table'", (table,)
    ).fetchone():
        return None
    row = db.execute(
        "SELECT descriptor_json FROM "
        + table
        + " WHERE source_key=? AND stage='prepare' AND generation=?",
        (job["source_key"], job["generation"]),
    ).fetchone()
    if not row:
        return None
    descriptor = json.loads(row[0])
    request = descriptor.get("request") or {}
    if descriptor.get("source_key") != job["source_key"] or not request.get(
        "source_url"
    ):
        return None
    mode = media._mode(artifact.get("profile"))
    if not mode:
        return None
    contract = {}
    trim = media._positive(request.get("source_trim_tail_seconds"))
    if trim:
        contract["trim_tail_seconds"] = trim
    original_sha = request.get("source_sha256") or ""
    if (
        channel == "x"
        and request.get("random_request")
        and artifact.get("source_sha256")
        and artifact["source_sha256"] != original_sha
    ):
        # The overlay may have been rendered from an intermediate repair. Use
        # that exact input only if proven; never attribute a trim to the full asset.
        return None
    if mode == "repair" and request.get("repair_request"):
        contract["duration_policy"] = request["repair_request"].get("duration_policy")
    if mode in ("direct_outro", "branded_preview"):
        assets = artifact.get("assets") or {}
        if assets:
            contract["assets"] = {
                k: v for k, v in assets.items() if k.endswith("_sha256")
            }
    return media.build_context(
        request["source_url"], mode=mode, contract=contract, source_sha256=original_sha
    )


def run(config, dry_run=False, batch=100):
    paths = dict(config["paths"])
    if config.get("x_tag", {}).get("paths", {}).get("x"):
        paths["x_tag"] = config["x_tag"]["paths"]["x"]
    path = Path(config["db_path"]).resolve()
    c = sqlite3.connect(
        path.as_uri() + ("?mode=ro" if dry_run else "?mode=rw"), uri=True, timeout=10
    )
    c.row_factory = sqlite3.Row
    pool = {}
    count, matched, cursor = 0, 0, 0
    try:
        if not dry_run:
            with c:
                media.ensure_schema(c)
                c.execute(
                    "CREATE TABLE IF NOT EXISTS pipeline_media_bootstrap (id INTEGER PRIMARY KEY,last_id INTEGER NOT NULL)"
                )
                checkpoint = c.execute(
                    "SELECT last_id FROM pipeline_media_bootstrap WHERE id=1"
                ).fetchone()
                cursor = checkpoint[0] if checkpoint else 0
        upper = c.execute("SELECT coalesce(max(id),0) FROM pipeline_job").fetchone()[0]
        while cursor < upper:
            rows = c.execute(
                """SELECT id,source_key,source_id,channel,family,generation,artifact_json,finished_at FROM pipeline_job
                WHERE id>? AND id<=? AND stage='prepare' AND status='completed' AND node_id<>'' AND generation>0
                AND channel IN ('fb','x','tt') AND json_extract(artifact_json,'$.mime')='video/mp4'
                AND length(json_extract(artifact_json,'$.sha256'))=64 ORDER BY id LIMIT ?""",
                (cursor, upper, batch),
            ).fetchall()
            records = []
            for job in rows:
                db = native_db(
                    paths, job["channel"], job["family"], pool, job["source_id"]
                )
                if db is None:
                    continue
                artifact = json.loads(job["artifact_json"])
                context = original_context(job, db, artifact)
                if context and (context["source_identity"] or context["source_sha256"]):
                    records.append((job, artifact, context))
            cursor = rows[-1]["id"] if rows else upper
            count += len(rows)
            if dry_run:
                matched += len(records)
                continue
            with c:
                for job, artifact, context in records:
                    # An existing context may contain target eligibility facts
                    # recorded since startup. Historical work cannot replace it.
                    c.execute(
                        "INSERT OR IGNORE INTO pipeline_media_context VALUES(?,?,?)",
                        (job["source_key"], job["channel"], media._json(context)),
                    )
                    if media.record_artifact(
                        c,
                        job["source_key"],
                        job["channel"],
                        artifact,
                        job["finished_at"],
                    ):
                        matched += 1
                c.execute(
                    "INSERT OR REPLACE INTO pipeline_media_bootstrap VALUES(1,?)",
                    (cursor,),
                )
            time.sleep(0.025)
        return {
            "examined": count,
            "matched": matched,
            "last_id": cursor,
            "dry_run": dry_run,
        }
    finally:
        for db in pool.values():
            db.close()
        c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/pipeline-coordinator.json")
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(json.loads(args.config.read_text()), args.dry_run)))
