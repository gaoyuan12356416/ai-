"""Durable, owner-scoped imports into the independent editor's private assets."""

import hashlib
import json
import logging
import os
import threading
import time
from urllib.parse import urljoin

import requests

from .catalog import decode_key, safe_url, PRODUCTS
from .runtime import Fault, MAX_BYTES, check, ident, owner, public_asset

LOG = logging.getLogger(__name__)
_started = set()
_start_lock = threading.Lock()


def setup(rt):
    with rt.db(True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS catalog_imports (
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,product TEXT NOT NULL,source_key TEXT NOT NULL,
            episode INTEGER NOT NULL,status TEXT NOT NULL,asset_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',bytes INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,lease TEXT NOT NULL DEFAULT '',expires REAL NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,
            UNIQUE(owner,product,source_key,episode))""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS catalog_imports_pending ON catalog_imports(status,expires,created)"
        )
        columns = {r[1] for r in db.execute('PRAGMA table_info(catalog_imports)')}
        for name, definition in [('stage', "TEXT NOT NULL DEFAULT 'queued'"), ('speed', 'REAL NOT NULL DEFAULT 0'), ('timings', "TEXT NOT NULL DEFAULT '{}'"), ('started','REAL NOT NULL DEFAULT 0')]:
            if name not in columns:
                db.execute('ALTER TABLE catalog_imports ADD COLUMN '+name+' '+definition)


def dto(row):
    return {
        k: row[k]
        for k in (
            "id",
            "product",
            "source_key",
            "episode",
            "status",
            "asset_id",
            "title",
            "error",
            "bytes",
            "total",
            "created",
            "updated",
            "stage",
            "speed",
            "timings",
        )
    }


def submit(rt, actor, body):
    with rt.db(True) as db:
        return submit_rows(db, actor, body)


def submit_rows(db, actor, body):
    product, key, numbers = body.get("product"), body.get("key"), body.get("episodes")
    decode_key(key, product)
    check(
        isinstance(numbers, list)
        and 1 <= len(numbers) <= 20
        and all(type(n) is int and 0 < n <= 100000 for n in numbers)
        and len(set(numbers)) == len(numbers),
        "catalog_selection",
        "每次请选择 1–20 集",
    )
    who, now = owner(actor), time.time()
    active = db.execute(
        "SELECT count(*) FROM catalog_imports WHERE owner=? AND status IN ('queued','downloading')",
        (who,),
    ).fetchone()[0]
    rows = []
    for n in numbers:
        previous = db.execute(
            "SELECT * FROM catalog_imports WHERE owner=? AND product=? AND source_key=? AND episode=?",
            (who, product, key, n),
        ).fetchone()
        if not previous:
            check(
                active < 20,
                "catalog_queue_full",
                "已有 20 集正在导入，请等待完成",
                429,
            )
            active += 1
            jid = ident("ci")
            db.execute(
                "INSERT INTO catalog_imports(id,owner,product,source_key,episode,status,created,updated) VALUES(?,?,?,?,?,'queued',?,?)",
                (jid, who, product, key, n, now, now),
            )
            previous = db.execute(
                "SELECT * FROM catalog_imports WHERE id=?", (jid,)
            ).fetchone()
        rows.append(dto(previous))
    return {"items": rows}


def list_imports(rt, actor):
    with rt.db() as db:
        rows = db.execute(
            "SELECT * FROM catalog_imports WHERE owner=? ORDER BY created DESC LIMIT 100",
            (owner(actor),),
        ).fetchall()
    return {"items": [dto(r) for r in rows]}


def retry(rt, actor, jid):
    with rt.db(True) as db:
        row = db.execute(
            "SELECT * FROM catalog_imports WHERE id=? AND owner=?", (jid, owner(actor))
        ).fetchone()
        check(row, "not_found", "导入任务不存在", 404)
        check(
            row["status"] == "failed",
            "catalog_retry_invalid",
            "只有失败的导入任务可以重试",
            409,
        )
        active = db.execute(
            "SELECT count(*) FROM catalog_imports WHERE owner=? AND status IN ('queued','downloading')",
            (owner(actor),),
        ).fetchone()[0]
        check(active < 20, "catalog_queue_full", "导入队列已满", 429)
        db.execute(
            "UPDATE catalog_imports SET status='queued',stage='queued',error='',attempts=0,bytes=0,total=0,speed=0,updated=? WHERE id=?",
            (time.time(), jid),
        )
    return {"ok": True}


def claim(rt):
    now = time.time()
    with rt.db(True) as db:
        db.execute(
            "UPDATE catalog_imports SET status='failed',error='导入多次中断，请重试',updated=? WHERE status='downloading' AND expires<? AND attempts>=3",
            (now, now),
        )
        row = db.execute(
            "SELECT * FROM catalog_imports WHERE status='queued' OR (status='downloading' AND expires<? AND attempts<3) ORDER BY created,episode LIMIT 1",
            (now,),
        ).fetchone()
        if not row:
            return None
        lease = ident("lease")
        db.execute(
            "UPDATE catalog_imports SET status='downloading',stage='resolving',lease=?,expires=?,attempts=attempts+1,updated=?,started=? WHERE id=?",
            (lease, now + 120, now, now, row["id"]),
        )
        return dict(row, lease=lease)


def progress(rt, job, count, total, title=""):
    with rt.db(True) as db:
        previous = db.execute('SELECT bytes,updated,speed FROM catalog_imports WHERE id=?',(job['id'],)).fetchone()
        elapsed = max(.01,time.time()-previous['updated'])
        speed = max(0,(count-previous['bytes'])/elapsed)
        if previous['speed'] and count >= previous['bytes']:
            speed = previous['speed']*.6+speed*.4
        changed = db.execute(
            "UPDATE catalog_imports SET bytes=?,total=?,title=?,updated=?,expires=?,speed=?,stage='downloading' WHERE id=? AND lease=? AND status='downloading'",
            (
                count,
                total,
                title,
                time.time(),
                time.time() + 120,
                speed,
                job["id"],
                job["lease"],
            ),
        ).rowcount
        check(changed, "catalog_lease_lost", "导入任务已转交", 409)


def download(url, path, callback):
    digest, count, started, updated = hashlib.sha256(), 0, time.monotonic(), 0
    with requests.Session() as http:
        http.trust_env = False
        for _ in range(4):
            response = http.get(
                safe_url(url), stream=True, timeout=(10, 30), allow_redirects=False
            )
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                continue
            break
        with response:
            check(
                response.status_code == 200,
                "catalog_download_failed",
                "源视频暂不可下载，请重试刷新地址",
                502,
            )
            total = int(response.headers.get("Content-Length", 0))
            check(
                0 <= total <= MAX_BYTES,
                "catalog_media_size",
                "该集超过 1 GiB，请改用较小文件",
                413,
            )
            with path.open("wb") as f:
                for chunk in response.iter_content(1024 * 256):
                    count += len(chunk)
                    check(
                        count <= MAX_BYTES and time.monotonic() - started < 600,
                        "catalog_download_limit",
                        "该集下载超时或超过 1 GiB，请重试",
                        413,
                    )
                    f.write(chunk)
                    digest.update(chunk)
                    if time.monotonic() - updated > 1:
                        callback(count, total)
                        updated = time.monotonic()
            check(
                count > 0 and (not total or total == count),
                "catalog_download_incomplete",
                "源视频下载不完整，请重试",
                502,
            )
            callback(count, total or count)
    return digest.hexdigest()


def process(rt, catalog, job):
    temporary = rt.root / "temp" / (job["id"] + "-" + job["lease"] + ".mp4")
    done = threading.Event()
    def beat():
        while not done.wait(20):
            try:
                with rt.db(True) as db:
                    if not db.execute("UPDATE catalog_imports SET expires=? WHERE id=? AND lease=? AND status='downloading'",(time.time()+120,job['id'],job['lease'])).rowcount:
                        return
            except Exception:
                LOG.warning('Import heartbeat delayed %s',job['id'])
    pulse=threading.Thread(target=beat,daemon=True);pulse.start()
    started=time.monotonic(); timings={'queue_ms':round(max(0,time.time()-job['created'])*1000)}
    try:
        rows = catalog.episodes(job["product"], job["source_key"], private=True)[
            "items"
        ]
        episode = next((r for r in rows if r["number"] == job["episode"]), None)
        timings['resolve_ms']=round((time.monotonic()-started)*1000)
        check(episode, "catalog_episode_missing", "该分集已不可用，请重新选集", 404)
        title = f"{episode['name']} · 第 {job['episode']} 集"
        progress(rt, job, 0, 0, title)
        download_start=time.monotonic()
        for attempt in range(3):
            try:
                digest = download(episode['url'],temporary,lambda n,total: progress(rt,job,n,total,title))
                break
            except (requests.RequestException,Fault) as exc:
                if attempt==2 or (isinstance(exc,Fault) and exc.code not in ('catalog_download_failed','catalog_download_incomplete')):
                    raise
                time.sleep((2,8)[attempt])
                if hasattr(catalog,'invalidate_episodes'):
                    catalog.invalidate_episodes(job['product'],job['source_key'])
                rows=catalog.episodes(job['product'],job['source_key'],private=True)['items']
                episode=next((r for r in rows if r['number']==job['episode']),None)
                check(episode,'catalog_episode_missing','该分集已不可用',404)
        timings['download_ms']=round((time.monotonic()-download_start)*1000)
        with rt.db(True) as db:
            changed=db.execute("UPDATE catalog_imports SET stage='validating',timings=? WHERE id=? AND lease=?",(json.dumps(timings),job['id'],job['lease'])).rowcount
            check(changed,'catalog_lease_lost','导入任务已转交',409)
        validate_start=time.monotonic()
        tenant, user = json.loads(job["owner"])
        series = (
            f"{PRODUCTS[job['product']]} · {episode['name']} · {episode['language']}"[
                :180
            ]
        )
        asset = rt.import_asset(
            temporary,
            digest,
            title[:230] + ".mp4",
            series,
            {"tenant_key": tenant, "user_id": user},
            job["episode"],
        )
        with rt.db(True) as db:
            timings['validate_ms']=round((time.monotonic()-validate_start)*1000)
            db.execute(
                "UPDATE catalog_imports SET status='ready',stage='ready',asset_id=?,error='',updated=?,timings=?,lease='',expires=0 WHERE id=? AND lease=?",
                (asset["id"], time.time(),json.dumps(timings), job["id"], job["lease"]),
            )
    except Exception as exc:
        message = str(exc) if isinstance(exc, Fault) else "导入未完成，请重试"
        LOG.warning(
            "Catalog import failed id=%s kind=%s", job["id"], type(exc).__name__
        )
        with rt.db(True) as db:
            db.execute(
                "UPDATE catalog_imports SET status='failed',stage='failed',error=?,updated=?,lease='',expires=0 WHERE id=? AND lease=?",
                (message, time.time(), job["id"], job["lease"]),
            )
    finally:
        done.set();pulse.join(1)
        temporary.unlink(missing_ok=True)
        from .source_jobs import notify
        notify(rt)


def start(rt, catalog):
    setup(rt)
    with _start_lock:
        if str(rt.db_path) in _started:
            return
        _started.add(str(rt.db_path))

    def worker():
        while True:
            try:
                job = claim(rt)
                if job:
                    process(rt, catalog, job)
                    continue
            except Exception:
                LOG.exception("Catalog importer loop failed")
            time.sleep(2)

    for i in range(max(1,min(3,int(os.environ.get('AI_VIDEO_EDITING_IMPORT_WORKERS','3'))))):
        threading.Thread(target=worker, name="editor-catalog-import-"+str(i), daemon=True).start()


def dispatch(rt, catalog, actor, verb, path, params, body):
    def q(name, default=""):
        return params.get(name, [default])[0]

    if path == "/catalog/dramas" and verb == "GET":
        return catalog.search(q("product"), q("q").strip(), int(q("page", "1")))
    if path == "/catalog/episodes" and verb == "GET":
        return catalog.episodes(q("product"), q("key"))
    if path == "/catalog/imports":
        if verb == "GET":
            return list_imports(rt, actor)
        if verb == "POST":
            return submit(rt, actor, body)
    if (
        path.startswith("/catalog/imports/")
        and path.endswith("/retry")
        and verb == "POST"
    ):
        return retry(rt, actor, path.split("/")[-2])
    raise Fault("not_found", "接口不存在", 404)
