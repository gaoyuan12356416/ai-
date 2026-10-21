"""Read-only FB source adapters and a report-owned immutable identity ledger.

No publisher imports, source writes, Graph calls, or source-schema migrations.
The collector alone writes the ledger; HTTP readers use SQLite mode=ro.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DEFAULT_FB_DB = "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3"
DEFAULT_USERS_DB = "/root/drama_material_service/data/drama_material_jobs.sqlite3"
DEFAULT_LEDGER = "/mnt/data-disk/social-analytics/attribution.sqlite3"
VERSION = "facebook-report-attribution-v1"
SITES = ("2049", "2310")
MAX_ROWS = 250000
PARAMETERS = {"c", "af_adset", "af_adset_id", "af_ad", "af_ad_id", "af_channel", "af_c_id", "af_dp"}


class AttributionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def timestamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise AttributionError("timestamp_invalid") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AttributionError("timestamp_invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def day(value):
    return timestamp(value)[:10]


def required(value):
    if isinstance(value, bool) or value is None:
        raise AttributionError("identity_field_invalid")
    result = str(value)
    if not result or result != result.strip() or len(result) > 4096 or any(ord(c) < 32 for c in result):
        raise AttributionError("identity_field_invalid")
    return result


def obj(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        raise AttributionError("snapshot_json_invalid") from None
    if not isinstance(result, dict):
        raise AttributionError("snapshot_json_invalid")
    return result


def same(*values):
    values = [required(value) for value in values]
    if len(set(values)) != 1:
        raise AttributionError("source_identity_mismatch")
    return values[0]


def take(row, names):
    return {key: row.get(key) for key in names.split()}


@contextmanager
def readonly(path):
    deadline = time.monotonic() + 45
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def rows(conn, table, columns, where=""):
    # All identifiers and predicates come from fixed source contracts below.
    result = [dict(row) for row in conn.execute(
        "SELECT " + columns + " FROM " + table + (" WHERE " + where if where else "") +
        " LIMIT " + str(MAX_ROWS + 1))]
    if len(result) > MAX_ROWS:
        raise AttributionError("source_capacity_exceeded")
    return result


def index(records, key):
    result = {}
    for record in records:
        token = str(record[key])
        if token in result:
            raise AttributionError("source_duplicate_identity")
        result[token] = record
    return result


def users_snapshot(path):
    with readonly(path) as conn:
        records = rows(conn, "drama_admin_user", "user_id,tenant_key,name")
    result = defaultdict(list)
    for record in records:
        result[str(record["user_id"])].append(record)
    return result


def user_identity(users, user_id, tenant=None):
    user_id = required(user_id)
    matches = users.get(user_id, [])
    if tenant is not None:
        tenant = required(tenant)
        matches = [row for row in matches if row["tenant_key"] == tenant]
    if len(matches) != 1 or not matches[0].get("tenant_key"):
        raise AttributionError("owner_tenant_not_unique")
    row = matches[0]
    return dict(owner=user_id, tenant=required(row["tenant_key"]),
                owner_label=str(row.get("name") or user_id))


def parse_link(url, site, task_id):
    try:
        parsed = urlsplit(required(url))
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30, strict_parsing=True)
        path = "/ads/0/2049/view" if site == "2049" else "/ads/101/2310/view"
        if (parsed.scheme != "https" or parsed.hostname != "www.dramawavew2a.com"
                or parsed.port is not None or parsed.username or parsed.password or parsed.fragment
                or parsed.path != path or set(query) != PARAMETERS
                or any(len(value) != 1 or not value[0] for value in query.values())):
            raise ValueError()
        result = {key: value[0] for key, value in query.items()}
        same(result["af_c_id"], task_id)
        for value in result.values():
            required(value)
        return result
    except (ValueError, TypeError):
        raise AttributionError("link_identity_invalid") from None


def _read_old(conn):
    tasks = rows(conn, "fb_auto_task", "id,run_id,template_id,template_version,page_id,material_id,"
                 "content_id,status,long_url,short_url,graph_post_id,unknown_outcome,created_at_utc,completed_at_utc")
    templates = index(rows(conn, "fb_auto_template",
                           "id,owner_user_id,created_by,created_name,created_at_utc"), "id")
    versions = index(rows(conn, "fb_auto_template_version",
                          "template_id,created_by,created_at_utc", "version=1"), "template_id")
    runs = index(rows(conn, "fb_auto_run", "id,template_id,template_version,trigger_type"), "id")
    ledgers = index(rows(conn, "fb_auto_publish_ledger",
                        "task_id,page_id,material_id,status,graph_post_id,unknown_outcome,updated_at_utc"), "task_id")
    result = []
    for task in tasks:
        if not task["long_url"]:
            # Not every legacy task generates a link (e.g. skipped/no-url
            # templates). A confirmed no-link publication is reported as a
            # source exception rather than disappearing into a false zero.
            if task["status"] == "published":
                raise AttributionError("published_task_without_link")
            continue
        evidence = dict(task=take(task, "id run_id template_id template_version page_id material_id "
                                  "content_id long_url short_url created_at_utc"),
                        template=templates.get(str(task["template_id"])),
                        version1=versions.get(str(task["template_id"])),
                        run=runs.get(str(task["run_id"])))
        facts = dict(task=take(task, "status graph_post_id unknown_outcome completed_at_utc"),
                     ledger=ledgers.get(str(task["id"])))
        result.append(dict(record_id=str(task["id"]), evidence=evidence, facts=facts))
    return result


def _read_new(conn):
    tasks = rows(conn, "fb_video_task", "task_id,batch_id,tenant_key,owner_user_id,page_id,material_id,"
                 "content_id,status,snapshot_json,ledger_id,long_url,short_url,graph_post_id,unknown_outcome,"
                 "created_at_utc,updated_at_utc")
    links = index(rows(conn, "fb_video_link", "id,task_id,long_url,short_url,created_at_utc"), "id")
    batches = index(rows(conn, "fb_video_batch",
                         "batch_id,tenant_key,owner_user_id,owner_name,operation_id,created_at_utc"), "batch_id")
    result = []
    for task in tasks:
        # Strip media URLs and unrelated source fields before hashing/storage.
        try:
            saved = obj(task["snapshot_json"])
            saved = {key: take(obj(saved.get(key)), fields) for key, fields in (
                ("actor", "tenant_key user_id user_name business_owner_id sub_user_id"),
                ("attribution", "task_id page_id page_name material_id material_name content_id "
                                "language drama_name tag sub_user_id"),
                ("page", "page_id page_name"),
                ("material", "material_id material_name content_id language drama_name"))}
        except AttributionError:
            saved = {"invalid_snapshot_hash": digest(task["snapshot_json"])}
        evidence = dict(task=take(task, "task_id batch_id tenant_key owner_user_id page_id material_id "
                                  "content_id ledger_id long_url short_url created_at_utc"),
                        snapshot=saved, link=links.get(str(task["ledger_id"])),
                        batch=batches.get(str(task["batch_id"])))
        facts = take(task, "status graph_post_id unknown_outcome updated_at_utc")
        result.append(dict(record_id=str(task["task_id"]), evidence=evidence, facts=facts))
    if {str(task["ledger_id"]) for task in tasks} != set(links):
        raise AttributionError("orphan_link_ledger")
    return result


def source_snapshot(path):
    """Independent failures: a missing/new schema must not erase legacy rows."""
    result, failures = {}, {}
    try:
        with readonly(path) as conn:
            for site, reader in (("2049", _read_old), ("2310", _read_new)):
                try:
                    result[site] = reader(conn)
                except Exception as exc:
                    failures[site] = exc.code if isinstance(exc, AttributionError) else "source_read_failed"
    except Exception:
        failures = dict.fromkeys(SITES, "source_read_failed")
    return result, failures


def _base(site, record, fields, owner, link_type):
    task = record["evidence"]["task"]
    record_id = record["record_id"]
    created = task["created_at_utc"]
    return dict(platform="facebook", platform_label="Facebook", site_id=site,
                key=[fields["af_c_id"], fields["c"]], link_id="facebook:" + site + ":" + record_id,
                channel="facebook:" + fields["af_adset_id"], channel_label=fields["af_adset"],
                drama=fields["af_dp"], drama_label=fields.get("drama_name") or fields["af_dp"],
                language=fields.get("language") or "未记录", language_label=fields.get("language") or "未记录",
                link_type=link_type, link_type_label="手动发布" if site == "2310" else "自动模板",
                date=day(created), excluded=False, **owner)


def identity_old(record, users):
    evidence = record["evidence"]
    task, template, version, run = (obj(evidence[key]) for key in ("task", "template", "version1", "run"))
    same(task["template_id"], template["id"], version["template_id"], run["template_id"])
    same(task["template_version"], run["template_version"])
    same(task["run_id"], run["id"])
    if not re.fullmatch(r"[1-9][0-9]{0,18}", str(task["id"])):
        raise AttributionError("task_id_invalid")
    creator = same(template["created_by"], version["created_by"])
    same(template["created_at_utc"], version["created_at_utc"])
    fields = parse_link(task["long_url"], "2049", task["id"])
    same(fields["af_adset_id"], task["page_id"])
    same(fields["af_ad_id"], task["material_id"])
    same(fields["af_dp"], task["content_id"])
    # No parsing of campaign names to infer people/dramas/accounts. Display
    # labels may use the frozen campaign segments, after ID validation only.
    segments = fields["c"].split("*")
    if len(segments) == 5:
        fields["drama_name"] = segments[2]
        match = re.fullmatch(r"[1-9][0-9]*none(.+)", segments[1])
        if match:
            fields["language"] = match[1]
    owner = user_identity(users, creator)
    # business owner 248 is evidence attached to this original creator, never
    # a fallback that maps arbitrary unresolved FB traffic to 郜远.
    owner["sub_user_id"] = str(template.get("owner_user_id") or "")
    return _base("2049", record, fields, owner, "auto_template")


def identity_new(record, users):
    evidence = record["evidence"]
    task, saved, link, batch = (obj(evidence[key]) for key in ("task", "snapshot", "link", "batch"))
    actor, context, page, material = (obj(saved.get(key)) for key in ("actor", "attribution", "page", "material"))
    task_id = same(task["task_id"], link["task_id"], context["task_id"])
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise AttributionError("task_id_invalid")
    same(task["ledger_id"], link["id"])
    same(task["batch_id"], batch["batch_id"])
    owner_id = same(task["owner_user_id"], actor["user_id"], batch["owner_user_id"])
    tenant = same(task["tenant_key"], actor["tenant_key"], batch["tenant_key"])
    same(task["long_url"], link["long_url"])
    same(task["short_url"], link["short_url"])
    same(task["created_at_utc"], link["created_at_utc"], batch["created_at_utc"])
    same(task["page_id"], context["page_id"], page["page_id"])
    same(task["material_id"], context["material_id"], material["material_id"])
    same(task["content_id"], context["content_id"], material["content_id"])
    same(context["page_name"], page["page_name"])
    for field in ("material_name", "language", "drama_name"):
        same(context[field], material[field])
    sub_user = same(actor["sub_user_id"], context["sub_user_id"])
    if not re.fullmatch(r"[1-9][0-9]*", sub_user):
        raise AttributionError("publisher_mapping_invalid")
    fields = parse_link(task["long_url"], "2310", task_id)
    for parameter, source in (("af_adset_id", "page_id"), ("af_adset", "page_name"),
                              ("af_ad_id", "material_id"), ("af_dp", "content_id"),
                              ("af_channel", "sub_user_id")):
        same(fields[parameter], context[source])
    same(fields["af_ad"], context["material_name"] + "_contentid[" + context["content_id"] + "]")
    created = datetime.fromisoformat(timestamp(task["created_at_utc"]).replace("Z", "+00:00"))
    campaign = "yingliang_post_CLV_VL_facebook_%s*%snone%s*%s*%s*%s" % (
        context["page_id"], int(created.timestamp()), context["language"], context["drama_name"],
        context.get("tag") or "none", task["ledger_id"])
    same(fields["c"], campaign)
    owner = user_identity(users, owner_id, tenant)
    owner["owner_label"] = same(actor["user_name"], batch["owner_name"])
    owner["sub_user_id"] = sub_user
    fields.update(drama_name=context["drama_name"], language=context["language"])
    return _base("2310", record, fields, owner, "manual")


def publications(site, record):
    facts, evidence = record["facts"], record["evidence"]
    task = facts["task"] if site == "2049" else facts
    known_statuses = {"planned", "preparing", "ready", "queued", "running", "submitted", "unknown",
                      "published", "failed", "failed_without_retry", "skipped", "cancelled"}
    if task["status"] not in known_statuses or task["unknown_outcome"] not in (0, 1):
        raise AttributionError("publication_status_invalid")
    if task["status"] != "published":
        return []
    post_id = required(task["graph_post_id"])
    if task["unknown_outcome"] != 0 or not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", post_id):
        raise AttributionError("publication_not_confirmed")
    if site == "2049":
        ledger = obj(facts["ledger"])
        same(ledger["task_id"], record["record_id"])
        same(ledger["page_id"], evidence["task"]["page_id"])
        same(ledger["material_id"], evidence["task"]["material_id"])
        same(ledger["graph_post_id"], post_id)
        if ledger["status"] != "published" or ledger["unknown_outcome"] != 0:
            raise AttributionError("publication_ledger_mismatch")
        published_day = day(task["completed_at_utc"])
        if published_day != day(ledger["updated_at_utc"]):
            raise AttributionError("publication_time_mismatch")
    else:
        # This schema has no published_at: terminal updated_at is the confirmed
        # reconciliation time; never substitute submission/creation time.
        published_day = day(task["updated_at_utc"])
    page_id = required(evidence["task"]["page_id"])
    # Graph may return either object ID or PageID_objectID. Normalize the same
    # Page/object across old/new sources, without collapsing two different Pages.
    object_id = post_id
    if "_" in post_id:
        post_page, object_id = post_id.split("_", 1)
        if post_page != page_id:
            raise AttributionError("publication_page_mismatch")
    return [dict(id="facebook:post:" + page_id + ":" + object_id, date=published_day, kind="post")]


def _private_open(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise AttributionError("ledger_path_invalid")
        if os.name != "nt":
            # Only remove rights on an existing file, never broaden them.
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) & 0o600)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def collector_lock(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.fdopen(_private_open(path), "r+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise AttributionError("collector_busy") from None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AttributionError("collector_busy") from None
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS social_facebook_schema(version TEXT NOT NULL PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS social_facebook_identity(
          site_id TEXT NOT NULL,record_id TEXT NOT NULL,version TEXT NOT NULL,
          identity_json TEXT NOT NULL,evidence_json TEXT NOT NULL,evidence_hash TEXT NOT NULL,
          source_hash TEXT NOT NULL,observed_hash TEXT NOT NULL,observed_campaign_id TEXT NOT NULL,
          publications_json TEXT NOT NULL,status TEXT NOT NULL,error_code TEXT NOT NULL,
          frozen_at TEXT NOT NULL,observed_at TEXT NOT NULL,status_at TEXT NOT NULL,
          PRIMARY KEY(site_id,record_id));
        CREATE TABLE IF NOT EXISTS social_facebook_source(
          site_id TEXT PRIMARY KEY,last_attempt_at TEXT NOT NULL,last_success_at TEXT NOT NULL,
          last_full_at TEXT NOT NULL,status TEXT NOT NULL,error_code TEXT NOT NULL,
          row_count INTEGER NOT NULL,conflict_count INTEGER NOT NULL,source_hash TEXT NOT NULL);
    """)
    existing = [row[0] for row in conn.execute("SELECT version FROM social_facebook_schema")]
    if not existing:
        conn.execute("INSERT INTO social_facebook_schema VALUES(?)", (VERSION,))
        conn.commit()
    elif existing != [VERSION]:
        raise AttributionError("ledger_version_unsupported")


def _pending(site, record):
    """Retain exact campaign ID for global conflict/pending accounting."""
    task = record["evidence"]["task"]
    try:
        fields = parse_link(task.get("long_url"), site, record["record_id"])
        return _base(site, record, fields, dict(owner="", tenant="", owner_label="待归属"),
                     "manual" if site == "2310" else "auto_template")
    except (AttributionError, KeyError):
        return {}


def _upsert_record(conn, site, record, users, now):
    record_id = record["record_id"]
    previous = conn.execute("SELECT * FROM social_facebook_identity WHERE site_id=? AND record_id=?",
                            (site, record_id)).fetchone()
    if previous and previous["version"] != VERSION:
        raise AttributionError("ledger_version_unsupported")
    evidence_hash, source_hash = digest(record["evidence"]), digest(record)
    observed_campaign = _pending(site, record).get("key", [""])[0]
    if previous and previous["source_hash"] == source_hash and previous["status"] == "frozen":
        return "reused"
    identity, status, error, frozen_at = {}, "frozen", "", now
    facts = []
    frozen_evidence, frozen_hash = canonical(record["evidence"]), evidence_hash
    if previous and previous["frozen_at"]:
        identity = obj(previous["identity_json"])
        frozen_at = previous["frozen_at"]
        frozen_evidence, frozen_hash = previous["evidence_json"], previous["evidence_hash"]
        if previous["status"] == "conflict" or evidence_hash != frozen_hash:
            status, error = "conflict", "frozen_evidence_changed"
    else:
        try:
            identity = (identity_old if site == "2049" else identity_new)(record, users)
        except (AttributionError, KeyError, TypeError) as exc:
            identity, status, frozen_at = _pending(site, record), "unresolved", ""
            error = exc.code if isinstance(exc, AttributionError) else "source_identity_invalid"
    try:
        facts = publications(site, record)
    except (AttributionError, KeyError, TypeError) as exc:
        status = "conflict" if frozen_at else "unresolved"
        error = exc.code if isinstance(exc, AttributionError) else "publication_identity_invalid"
    if previous and previous["frozen_at"] and previous["publications_json"] != "[]":
        if json.loads(previous["publications_json"]) != facts:
            status, error = "conflict", "confirmed_publication_changed"
            facts = json.loads(previous["publications_json"])
    conn.execute("""INSERT INTO social_facebook_identity VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(site_id,record_id) DO UPDATE SET
          identity_json=excluded.identity_json,evidence_json=excluded.evidence_json,
          evidence_hash=excluded.evidence_hash,source_hash=excluded.source_hash,
          observed_hash=excluded.observed_hash,observed_campaign_id=excluded.observed_campaign_id,
          publications_json=excluded.publications_json,status=excluded.status,error_code=excluded.error_code,
          frozen_at=excluded.frozen_at,observed_at=excluded.observed_at,status_at=excluded.status_at""",
                 (site, record_id, VERSION, canonical(identity), frozen_evidence, frozen_hash,
                  source_hash, evidence_hash, observed_campaign, canonical(facts), status, error,
                  frozen_at, now, now))
    return status


def collect(fb_db=DEFAULT_FB_DB, users_db=DEFAULT_USERS_DB, ledger=DEFAULT_LEDGER, *,
            now=None, mode="incremental", lock_path=None):
    """One atomic collection. Incremental writes only changed source hashes.

    Legacy tables have no mutation watermark, so both modes perform a bounded
    source scan; skipping that scan would miss edits to old identity evidence.
    """
    now = timestamp(now)
    if mode not in {"full", "incremental"}:
        raise AttributionError("collector_mode_invalid")
    ledger = Path(ledger).resolve()
    if ledger in {Path(fb_db).resolve(), Path(users_db).resolve()}:
        raise AttributionError("ledger_must_be_separate")
    lock_path = Path(lock_path).resolve() if lock_path else ledger.with_suffix(".collector.lock")
    if lock_path in {ledger, Path(fb_db).resolve(), Path(users_db).resolve()}:
        raise AttributionError("lock_must_be_separate")
    with collector_lock(lock_path):
        ledger.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(_private_open(ledger))
        conn = sqlite3.connect(ledger, timeout=10)
        conn.row_factory = sqlite3.Row
        schema_ready = False
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # SQLite inherits the DB mode for newly-created WAL/SHM files.
            # Tighten old sidecars too, without touching source/parent paths.
            if os.name != "nt":
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(ledger) + suffix)
                    if sidecar.exists():
                        os.close(_private_open(sidecar))
            schema(conn)
            schema_ready = True
            captured, failures = source_snapshot(fb_db)
            try:
                users = users_snapshot(users_db)
            except Exception:
                users = None
                failures.update(dict.fromkeys(SITES, "user_directory_read_failed"))
            conn.execute("BEGIN IMMEDIATE")
            summary = {}
            for site in SITES:
                old_state = conn.execute("SELECT * FROM social_facebook_source WHERE site_id=?", (site,)).fetchone()
                if site in failures:
                    count = old_state["row_count"] if old_state else 0
                    conflicts = old_state["conflict_count"] if old_state else 0
                    conn.execute("""INSERT INTO social_facebook_source VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(site_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                        status=excluded.status,error_code=excluded.error_code""",
                                 (site, now, old_state["last_success_at"] if old_state else "",
                                  old_state["last_full_at"] if old_state else "", "stale", failures[site],
                                  count, conflicts, old_state["source_hash"] if old_state else ""))
                    summary[site] = dict(status="stale", count=count, error=failures[site], changed=0)
                    continue
                records, changed = captured[site], 0
                ids = {record["record_id"] for record in records}
                if len(ids) != len(records):
                    raise AttributionError("source_duplicate_identity")
                for record in records:
                    changed += _upsert_record(conn, site, record, users, now) != "reused"
                for previous in conn.execute("SELECT record_id,status FROM social_facebook_identity WHERE site_id=?", (site,)).fetchall():
                    if previous["record_id"] not in ids and previous["status"] != "conflict":
                        conn.execute("""UPDATE social_facebook_identity SET status='conflict',
                            error_code='source_record_missing',status_at=? WHERE site_id=? AND record_id=?""",
                                     (now, site, previous["record_id"]))
                        changed += 1
                conflicts = conn.execute("SELECT COUNT(*) FROM social_facebook_identity WHERE site_id=? AND status<>'frozen'",
                                         (site,)).fetchone()[0]
                state = "partial" if conflicts else "complete"
                conn.execute("""INSERT INTO social_facebook_source VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(site_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at,last_full_at=excluded.last_full_at,
                    status=excluded.status,error_code=excluded.error_code,row_count=excluded.row_count,
                    conflict_count=excluded.conflict_count,source_hash=excluded.source_hash""",
                             (site, now, now, now if mode == "full" or old_state is None else old_state["last_full_at"],
                              state, "attribution_conflicts" if conflicts else "", len(records), conflicts,
                              digest([(record["record_id"], digest(record)) for record in records])))
                summary[site] = dict(status=state, count=len(records), conflicts=conflicts, changed=changed)
            conn.commit()
            return dict(status="complete" if all(v["status"] == "complete" for v in summary.values()) else "partial",
                        mode=mode, collected_at=now, sources=summary)
        except Exception:
            conn.rollback()
            # A failed transaction must not leave the previous successful
            # snapshot looking freshly collected. Preserve all frozen rows,
            # and commit only the failed-attempt marker where storage permits.
            try:
                if not schema_ready:
                    raise sqlite3.OperationalError("schema not ready")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""UPDATE social_facebook_source SET last_attempt_at=?,
                    status='stale',error_code='collection_transaction_failed'""", (now,))
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
            raise
        finally:
            conn.close()


def read_facebook(app):
    """Read safely frozen identities, with per-site completeness/freshness."""
    path = app.get("SOCIAL_ANALYTICS_FACEBOOK_ATTRIBUTION_DB",
                   app.get("SOCIAL_ANALYTICS_ATTRIBUTION_DB_PATH", DEFAULT_LEDGER))
    now = timestamp(app.get("SOCIAL_ANALYTICS_FACEBOOK_NOW"))
    try:
        max_age = int(app.get("SOCIAL_ANALYTICS_FACEBOOK_MAX_AGE_SECONDS", 300))
        if max_age <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        max_age = 300
    catalog, errors, metadata = [], {}, {}
    try:
        with readonly(path) as conn:
            if [row[0] for row in conn.execute("SELECT version FROM social_facebook_schema")] != [VERSION]:
                raise AttributionError("ledger_version_unsupported")
            for site in SITES:
                key = "facebook:" + site
                try:
                    state = conn.execute("SELECT * FROM social_facebook_source WHERE site_id=?", (site,)).fetchone()
                    if not state or not state["last_success_at"]:
                        raise AttributionError("source_never_collected")
                    age = (datetime.fromisoformat(now.replace("Z", "+00:00")) -
                           datetime.fromisoformat(timestamp(state["last_success_at"]).replace("Z", "+00:00"))).total_seconds()
                    fresh = 0 <= age <= max_age and state["status"] != "stale"
                    metadata[key] = dict(state="complete" if fresh and state["status"] == "complete" else "partial",
                                         freshness="fresh" if fresh else "stale",
                                         last_success_at=state["last_success_at"], last_attempt_at=state["last_attempt_at"],
                                         conflict_count=state["conflict_count"], row_count=state["row_count"],
                                         error=("归属证据存在冲突，已列待归属" if state["conflict_count"] else
                                                "归属采集过期或失败，使用上次安全快照" if not fresh else ""))
                    site_rows = []
                    for record in rows(conn, "social_facebook_identity", "*", "site_id='" + site + "'"):
                        if record["version"] != VERSION:
                            raise AttributionError("ledger_version_unsupported")
                        identity = obj(record["identity_json"])
                        if not identity:
                            continue
                        if identity["site_id"] != site or identity["platform"] != "facebook":
                            raise AttributionError("ledger_identity_invalid")
                        identity["publications"] = json.loads(record["publications_json"])
                        identity["attribution_status"] = record["status"]
                        if record["status"] == "frozen":
                            # Use immutable evidence, never today's Page owner or
                            # campaign text, to separate reused legacy IDs.
                            evidence = obj(record["evidence_json"])
                            fields = parse_link(evidence["task"]["long_url"], site, record["record_id"])
                            identity["effect_source"] = {
                                "adset_id": fields["af_adset_id"], "ad_id": fields["af_ad_id"]}
                        if record["status"] != "frozen":
                            # Keep confirmed facts so the common engine can
                            # expose publication conflicts instead of fake zero.
                            identity.update(owner="", owner_label="待归属")
                        site_rows.append(identity)
                    catalog.extend(site_rows)
                except Exception:
                    metadata[key] = dict(state="unavailable", freshness="stale",
                                         error="归属账本未同步或读取失败，待核实")
                    errors[key] = metadata[key]["error"]
    except Exception:
        for site in SITES:
            key = "facebook:" + site
            errors[key] = "归属账本未同步或读取失败，待核实"
            metadata[key] = dict(state="unavailable", freshness="stale", error=errors[key])
    return catalog, errors, metadata
