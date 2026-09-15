"""Complete, immutable Video -> Ad snapshots; partial streams never authorize writes.

One gated MySQL SELECT supplies a generation. No timestamp-delta reconstruction,
source DDL, background timer, token persistence, or Meta mutation is involved.
"""
from contextlib import closing
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from .core import AssetError, stored_ids, stored_ids_complete

FORMAT_VERSION = 2


class ReferenceRows(list):
    def __init__(self, rows=(), proof=None):
        super().__init__(rows)
        self.proof = proof


def video_ids_complete(raw):
    text = str(raw or "").strip()
    # A full VARCHAR(512) can be silently cut even when its last token is numeric.
    if len(str(raw or "")) >= 512:
        return False
    if re.fullmatch(r"0(?:\s*[,;]\s*0)*", text):
        return True  # explicit zero placeholders cannot identify a Meta node
    return stored_ids_complete(raw)


class MysqlVideoStream:
    def __init__(self, base_cmd, password, schema, timeout=600):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
            raise ValueError("invalid schema")
        self.cmd = list(base_cmd)
        if self.cmd[-1:] == ["-e"]:
            self.cmd.pop()
        if Path(self.cmd[0]).name != "mysql":
            raise ValueError("Video indexing must use the gated mysql client")
        self.password, self.schema, self.timeout = password, schema, timeout

    def __call__(self):
        env = os.environ.copy()
        env.update(MYSQL_PWD=self.password, SQL_GATE_BYPASS="0",
                   SQL_GATE_RETRY_DELAYS_SECONDS="", SQL_GATE_WAIT_TIMEOUT_SECONDS="30",
                   SQL_GATE_EXEC_TIMEOUT_SECONDS=str(self.timeout + 15),
                   MYSQL_QUERY_TIMEOUT_SECONDS=str(self.timeout),
                   MYSQL_QUERY_CONNECT_TIMEOUT_SECONDS="5")
        # JSON preserves source tabs/newlines/NULL without client escaping.
        sql = ("SELECT JSON_ARRAY('readonly',@@read_only);\n"
               "SELECT JSON_ARRAY(id,ad_id,product,ad_account_id,video_id) "
               "FROM `%s`.ads_facebook_auto_created_data FORCE INDEX(PRIMARY) "
               "WHERE (status IS NULL OR status<>'DELETED') AND video_id IS NOT NULL AND video_id<>'';\n"
               "SELECT JSON_ARRAY('complete');\n") % self.schema
        proc = subprocess.Popen(self.cmd + ["--quick", "--raw", "--unbuffered", "--skip-reconnect"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, text=True, encoding="utf-8")
        expired = threading.Event()
        def stop():
            expired.set()
            if proc.poll() is None:
                proc.terminate()  # gate forwards termination and reaps its owned session
        timer = threading.Timer(self.timeout + 45, stop)
        timer.daemon = True
        timer.start()
        try:
            proc.stdin.write(sql)
            proc.stdin.close()
            first, complete = True, False
            for line in proc.stdout:
                if len(line) > 16384:
                    raise ValueError("oversized source row")
                row = json.loads(line)
                if first:
                    if row != ["readonly", 1]:
                        raise ValueError("source read-only proof missing")
                    first = False
                elif row == ["complete"]:
                    if complete:
                        raise ValueError("duplicate completion marker")
                    complete = True
                else:
                    if complete or not isinstance(row, list) or len(row) != 5:
                        raise ValueError("invalid source stream")
                    yield row
            if proc.wait(timeout=10) != 0 or first or not complete or expired.is_set():
                raise ValueError("source stream incomplete")
        except Exception:
            raise AssetError("video_index_incomplete", "视频引用索引未完整读取，未解除阻止；请查看核验进度后重试", 503) from None
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()


class VideoIndex:
    def __init__(self, root, stream, max_age=600, clock=time.time, validate_storage=None):
        self.root = Path(root).resolve()
        self.stream, self.max_age, self.clock = stream, max_age, clock
        self.validate_storage = validate_storage or (lambda: None)
        self.path = self.root / "current.sqlite3"

    def _storage(self):
        self.validate_storage()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(str(self.root)).free < 2 * 1024**3:
            raise AssetError("video_index_disk_full", "视频引用索引数据盘剩余空间不足，停止核验", 503)

    def _proof(self):
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as conn:
                return json.loads(conn.execute("SELECT value FROM metadata WHERE key='proof'").fetchone()[0])
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return None

    def validate(self, proof):
        age = self.clock() - float((proof or {}).get("snapshot_started_at", 0))
        if not proof or proof.get("format_version") != FORMAT_VERSION or proof.get("complete") is not True or age < 0 or age >= self.max_age:
            raise AssetError("video_index_expired", "视频引用快照不完整或已过期，请重新核验；未执行该视频删除", 409)

    def build(self, progress=None):
        self._storage()
        # An OS-released SQLite write lock coordinates API and maintenance processes.
        lock = sqlite3.connect(str(self.root / "build-lock.sqlite3"), timeout=0, isolation_level=None)
        stage = self.root / ("building-" + uuid.uuid4().hex + ".sqlite3")
        conn = None
        stream = None
        started, last_report = self.clock(), 0
        count, relations, malformed = 0, 0, 0
        try:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                raise AssetError("video_index_building", "已有视频引用索引正在构建，请完成后重新核验；未发出删除请求", 409) from None
            conn = sqlite3.connect(str(stage))
            os.chmod(str(stage), 0o600)
            conn.executescript("""PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE ads(row_id INTEGER PRIMARY KEY,ad_id TEXT,product_id TEXT,account_id TEXT);
                CREATE TABLE refs(video_id TEXT NOT NULL,row_id INTEGER NOT NULL);
                CREATE TABLE malformed(row_id INTEGER PRIMARY KEY,ad_id TEXT,product_id TEXT,account_id TEXT,raw TEXT);
            """)
            stream = self.stream()
            ads, refs = [], []
            if progress:
                progress("video", 0, 0)
            for row in stream:
                if not isinstance(row, (list, tuple)) or len(row) != 5 or not str(row[0]).isdigit():
                    raise ValueError("invalid source row")
                rid, aid, product, account, raw = row
                count += 1
                if not video_ids_complete(raw):
                    malformed += 1
                    conn.execute("INSERT INTO malformed VALUES (?,?,?,?,?)", (int(rid), str(aid or ""), str(product or ""), str(account or ""), str(raw)))
                else:
                    ids = stored_ids(raw)
                    if ids:
                        ads.append((int(rid), str(aid or ""), str(product or ""), str(account or "")))
                        refs.extend((vid, int(rid)) for vid in ids)
                        relations += len(ids)
                if count % 5000 == 0:
                    conn.executemany("INSERT INTO ads VALUES (?,?,?,?)", ads)
                    conn.executemany("INSERT INTO refs VALUES (?,?)", refs)
                    conn.commit()
                    ads, refs = [], []
                    if self.clock() - started >= self.max_age:
                        raise AssetError("video_index_expired", "视频引用完整读取超过有效期，保持阻止；需进一步优化数据读取", 503)
                    if self.clock() - last_report >= 5:
                        self._storage()
                        if stage.stat().st_size > 30 * 1024**3:
                            raise AssetError("video_index_too_large", "视频引用索引超过 30 GiB 上限，已停止核验", 503)
                        if progress:
                            progress("video", count, 0)
                        last_report = self.clock()
            conn.executemany("INSERT INTO ads VALUES (?,?,?,?)", ads)
            conn.executemany("INSERT INTO refs VALUES (?,?)", refs)
            conn.execute("CREATE INDEX refs_video ON refs(video_id)")
            proof = dict(format_version=FORMAT_VERSION, generation_id=stage.stem.removeprefix("building-"), complete=True,
                         snapshot_started_at=started, completed_at=self.clock(), source_rows=count,
                         relations=relations, malformed_rows=malformed)
            self.validate(proof)
            conn.execute("INSERT INTO metadata VALUES ('proof',?)", (json.dumps(proof),))
            conn.commit()
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("index integrity check failed")
            conn.close()
            conn = None
            with stage.open("r+b") as handle:
                os.fsync(handle.fileno())
            self.validate(proof)
            os.replace(str(stage), str(self.path))
            if os.name != "nt":
                fd = os.open(str(self.root), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if progress:
                progress("video", count, count)
            return proof
        except AssetError:
            raise
        except Exception:
            raise AssetError("video_index_incomplete", "视频引用索引构建或写入失败，部分结果不能用于删除", 503) from None
        finally:
            if stream is not None and hasattr(stream, "close"):
                stream.close()
            if conn is not None:
                conn.close()
            lock.close()
            if stage.exists():
                stage.unlink()

    def references(self, ids, fresh=False, progress=None, resolver=None):
        self._storage()
        proof = self._proof()
        try:
            self.validate(proof)
        except AssetError:
            fresh = True
        if fresh:
            self.build(progress)
        result = ReferenceRows()
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as conn:
                # Read metadata and references from the SAME immutable file handle.
                proof = json.loads(conn.execute("SELECT value FROM metadata WHERE key='proof'").fetchone()[0])
                self.validate(proof)
                if proof["malformed_rows"]:
                    if resolver is None or proof["malformed_rows"] > 100:
                        raise AssetError("video_index_malformed", "全局视频引用存在 %s 条无法完整解析的记录，需核实历史广告实际关系" % proof["malformed_rows"], 409)
                    bad = [dict(zip(("row_id", "ad_id", "product_id", "account_id", "raw"), row))
                           for row in conn.execute("SELECT row_id,ad_id,product_id,account_id,raw FROM malformed LIMIT 101")]
                    if len(bad) != proof["malformed_rows"]:
                        raise AssetError("video_index_incomplete", "历史视频异常记录不完整，保持阻止", 503)
                    target_keys = {"video:" + v for v in ids}
                    result.extend(r for r in resolver(bad) if r["key"] in target_keys)
                for offset in range(0, len(ids), 250):
                    part = ids[offset:offset+250]
                    rows = conn.execute("SELECT r.video_id,a.ad_id,a.product_id,a.account_id FROM refs r JOIN ads a ON a.row_id=r.row_id WHERE r.video_id IN (" + ",".join("?" * len(part)) + ") LIMIT 100001", part).fetchall()
                    result.extend(dict(key="video:" + v, ad_id=a, product_id=p, account_id=c) for v, a, p, c in rows)
                    if len(result) > 100000:
                        raise AssetError("reference_check_incomplete", "共享引用超过核验上限，相关视频暂不能删除", 409)
                result.proof = proof
                self.validate(proof)
                return result
        except AssetError:
            raise
        except Exception:
            raise AssetError("video_index_unavailable", "视频引用索引无法完整读取，保持阻止", 503) from None
