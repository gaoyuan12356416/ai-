"""Audited, resumable link-only migration. Caller holds the publishing lock."""
import base64
import hashlib
import html
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from .attribution import SCHEMA, VERSION, AttributionError, build_url, readonly, valid_id, validate_context


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def redirect_bytes(target):
    escaped = html.escape(target, quote=True)
    js = json.dumps(target, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="referrer" content="no-referrer">'
            '<meta http-equiv="Cache-Control" content="no-store">'
            '<meta http-equiv="refresh" content="0;url=%s">'
            '<link rel="canonical" href="%s"><title>Redirecting</title>'
            '<script>location.replace(%s);</script></head>'
            '<body><a rel="noreferrer" href="%s">Continue</a></body></html>\n'
            % (escaped, escaped, js, escaped)).encode()


def target_from_bytes(data):
    source = data.decode("utf-8")
    match = re.search(r"location\.replace\((\"(?:\\.|[^\"\\])*\")\)", source)
    if not match:
        raise AttributionError("migration_redirect_target_invalid")
    return json.loads(match.group(1))


def atomic_bytes(path, content, mode=0o644):
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content); out.flush(); os.fsync(out.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        if os.name != "nt":
            directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def create_manifest(db_path, public_root, resolver, state_dir):
    root, state = Path(public_root).resolve(strict=True), Path(state_dir)
    if state.exists():
        raise AttributionError("migration_state_already_exists")
    records = []
    with closing(readonly(db_path)) as c:
        c.execute("BEGIN")
        logs = [dict(r) for r in c.execute("SELECT * FROM x_post_publish_log WHERE long_url<>'' ORDER BY id")]
        queues = {r["id"]: dict(r) for r in c.execute("SELECT * FROM x_post_queue")}
        has_table = c.execute("SELECT 1 FROM sqlite_master WHERE name='x_post_link_attribution' AND type='table'").fetchone()
        frozen = {r["log_id"]: dict(r) for r in c.execute("SELECT * FROM x_post_link_attribution")} if has_table else {}
        c.rollback()
    for log in logs:
        context = resolver.resolve(log["queue_id"], log["id"])
        new_url = build_url(context, log)
        path = root / (str(log["id"]) + ".html")
        if path.is_symlink() or not path.is_file():
            raise AttributionError("migration_redirect_missing")
        old_bytes = path.read_bytes()
        if target_from_bytes(old_bytes) != log["long_url"]:
            raise AttributionError("migration_redirect_drift")
        if log["long_url"] == new_url and log["id"] in frozen:
            continue
        new_bytes = redirect_bytes(new_url)
        records.append(dict(log=log, queue=queues[log["queue_id"]], old_frozen=frozen.get(log["id"]),
                            context=context, new_url=new_url, filename=path.name,
                            file_mode=path.stat().st_mode & 0o777,
                            old_bytes=base64.b64encode(old_bytes).decode(), new_bytes=base64.b64encode(new_bytes).decode(),
                            old_sha=sha(old_bytes), new_sha=sha(new_bytes)))
    manifest = dict(version=VERSION, db_path=str(Path(db_path).resolve()), public_root=str(root), records=records)
    payload = canonical(manifest).encode()
    state.mkdir(parents=True, exist_ok=False, mode=0o700)
    atomic_bytes(state / "manifest.json", payload, 0o600)
    atomic_bytes(state / "manifest.sha256", (sha(payload) + "\n").encode(), 0o600)
    return dict(count=len(records), manifest_sha256=sha(payload))


def load_manifest(state_dir, expected_hash):
    state = Path(state_dir)
    data = (state / "manifest.json").read_bytes()
    if not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash or "")) or sha(data) != expected_hash or (state / "manifest.sha256").read_text().strip() != expected_hash:
        raise AttributionError("migration_manifest_hash_mismatch")
    manifest = json.loads(data)
    if not isinstance(manifest, dict) or manifest.get("version") != VERSION:
        raise AttributionError("migration_version_invalid")
    if not isinstance(manifest.get("records"), list):
        raise AttributionError("migration_manifest_invalid")
    log_ids, queue_ids = set(), set()
    for entry in manifest["records"]:
        try:
            log_id, queue_id = valid_id(entry["log"]["id"]), valid_id(entry["log"]["queue_id"])
            if log_id in log_ids or queue_id in queue_ids or str(entry["queue"]["id"]) != queue_id:
                raise AttributionError("migration_manifest_duplicate_identity")
            log_ids.add(log_id); queue_ids.add(queue_id)
            validate_context(entry["context"], entry["log"])
            if build_url(entry["context"], entry["log"]) != entry["new_url"]:
                raise AttributionError("migration_manifest_url_invalid")
            old_bytes, new_bytes = base64.b64decode(entry["old_bytes"], validate=True), base64.b64decode(entry["new_bytes"], validate=True)
            if (sha(old_bytes) != entry["old_sha"] or sha(new_bytes) != entry["new_sha"]
                    or target_from_bytes(old_bytes) != entry["log"]["long_url"]
                    or target_from_bytes(new_bytes) != entry["new_url"]):
                raise AttributionError("migration_payload_hash_mismatch")
        except (KeyError, TypeError, ValueError):
            raise AttributionError("migration_manifest_invalid") from None
    return state, manifest


def _current(db, entry):
    """Compare all durable publication facts before every write and readback."""
    log_id = entry["log"]["id"]
    row = db.execute("SELECT * FROM x_post_publish_log WHERE id=?", (log_id,)).fetchone()
    queue = db.execute("SELECT * FROM x_post_queue WHERE id=?", (entry["log"]["queue_id"],)).fetchone()
    if row is None or queue is None or dict(queue) != entry["queue"]:
        raise AttributionError("migration_queue_drift")
    current = dict(row)
    if {k:v for k,v in current.items() if k != "long_url"} != {k:v for k,v in entry["log"].items() if k != "long_url"}:
        raise AttributionError("migration_publication_fact_drift")
    frozen = db.execute("SELECT * FROM x_post_link_attribution WHERE log_id=? OR queue_id=?",
                        (log_id, entry["log"]["queue_id"])).fetchall()
    if len(frozen) > 1 or (frozen and frozen[0]["log_id"] != log_id):
        raise AttributionError("migration_attribution_drift")
    return current, dict(frozen[0]) if frozen else None


def _expected_frozen(entry, rollback=False):
    if rollback:
        return entry["old_frozen"]
    return dict(log_id=entry["log"]["id"], queue_id=entry["log"]["queue_id"],
                version=VERSION, context_json=canonical(entry["context"]),
                created_at=entry["context"]["frozen_at"])


def _readback(db, entry, path, rollback):
    current, frozen = _current(db, entry)
    expected_url = entry["log"]["long_url"] if rollback else entry["new_url"]
    expected_sha = entry["old_sha"] if rollback else entry["new_sha"]
    if (current["long_url"] != expected_url or frozen != _expected_frozen(entry, rollback)
            or path.is_symlink() or sha(path.read_bytes()) != expected_sha
            or target_from_bytes(path.read_bytes()) != expected_url):
        raise AttributionError("migration_readback_failed")


def run(state_dir, expected_hash, rollback=False, fault=None):
    state, manifest = load_manifest(state_dir, expected_hash)
    root = Path(manifest["public_root"]).resolve(strict=True)
    if root != Path(manifest["public_root"]) or not root.is_dir():
        raise AttributionError("migration_path_invalid")
    db_path = Path(manifest["db_path"]).resolve(strict=True)
    with closing(sqlite3.connect(str(state / "progress.sqlite3"), timeout=10)) as journal:
        journal.execute("PRAGMA synchronous=FULL")
        journal.execute("CREATE TABLE IF NOT EXISTS progress(log_id INTEGER PRIMARY KEY, phase TEXT NOT NULL)")
        journal.commit()
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute(SCHEMA); db.commit()
            records = list(reversed(manifest["records"])) if rollback else manifest["records"]
            for entry in records:
                log_id = entry["log"]["id"]
                old_url, new_url = entry["log"]["long_url"], entry["new_url"]
                old_bytes, new_bytes = base64.b64decode(entry["old_bytes"]), base64.b64decode(entry["new_bytes"])
                path = root / entry["filename"]
                if path.parent != root or path.name != str(log_id) + ".html" or path.is_symlink() or not path.is_file():
                    raise AttributionError("migration_path_invalid")
                phase = "rollback_file" if rollback else "file_applied"
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    current, frozen = _current(db, entry)
                    if current["long_url"] not in {old_url, new_url} or sha(path.read_bytes()) not in {entry["old_sha"], entry["new_sha"]}:
                        raise AttributionError("migration_link_drift")
                    if frozen != entry["old_frozen"] and frozen != _expected_frozen(entry):
                        raise AttributionError("migration_attribution_drift")
                    atomic_bytes(path, old_bytes if rollback else new_bytes, entry.get("file_mode", 0o644))
                    journal.execute("INSERT OR REPLACE INTO progress VALUES(?,?)", (log_id, phase)); journal.commit()
                    if fault: fault(log_id, phase)
                    changed = db.execute("UPDATE x_post_publish_log SET long_url=? WHERE id=? AND long_url=?",
                                         (old_url if rollback else new_url, log_id, current["long_url"]))
                    if changed.rowcount != 1:
                        raise AttributionError("migration_database_write_conflict")
                    expected = _expected_frozen(entry, rollback)
                    if frozen != expected:
                        if expected is None:
                            db.execute("DELETE FROM x_post_link_attribution WHERE log_id=?", (log_id,))
                        elif frozen is None:
                            db.execute("INSERT INTO x_post_link_attribution(log_id,queue_id,version,context_json,created_at) VALUES(?,?,?,?,?)",
                                       tuple(expected[k] for k in ("log_id", "queue_id", "version", "context_json", "created_at")))
                        else:
                            db.execute("UPDATE x_post_link_attribution SET queue_id=?,version=?,context_json=?,created_at=? WHERE log_id=?",
                                       tuple(expected[k] for k in ("queue_id", "version", "context_json", "created_at", "log_id")))
                    _readback(db, entry, path, rollback)
                if fault: fault(log_id, "database_applied")
                _readback(db, entry, path, rollback)
                journal.execute("INSERT OR REPLACE INTO progress VALUES(?,?)", (log_id, "rolled_back" if rollback else "verified")); journal.commit()
    return dict(count=len(records), status="rolled_back" if rollback else "verified", manifest_sha256=expected_hash)
