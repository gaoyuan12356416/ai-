"""Read-only, namespace-aware evidence for the one-off FB Post cleanup.

No application stores are imported: constructing one can migrate its database.
Credentials are read only on demand and never enter freeze results.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
import threading
from typing import Mapping


UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
ALLOWED_GROUPS = (3, 4, 24, 25)
DEFAULT_AI_DB = "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3"
_CONNECTIONS = threading.BoundedSemaphore(2)
_ID = re.compile(r"[1-9][0-9]{0,30}\Z")
_SCHEMA = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
QUERY_LIMIT = 50000


class SourceError(RuntimeError):
    """Only fixed, non-secret diagnostic codes cross this module boundary."""


def identifier(value):
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise SourceError("invalid_numeric_identifier")
    return value


def utc(value, *, naive_timezone=None):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if naive_timezone is None:
                raise ValueError()
            parsed = parsed.replace(tzinfo=naive_timezone)
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise SourceError("invalid_evidence_timestamp") from None


def stamp(value):
    return utc(value).isoformat(timespec="seconds")


def chunks(values, size=100):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def slots(values):
    return ",".join("%s" for _ in values)


def post_aliases(page_id, values):
    """Lookup aliases only; these must never be used as DELETE identities."""
    result = set()
    for value in values:
        value = str(value or "").strip()
        if _ID.fullmatch(value):
            result.update((value, str(page_id) + "_" + value))
        elif value.startswith(str(page_id) + "_") and _ID.fullmatch(value.split("_", 1)[1]):
            result.update((value, value.split("_", 1)[1]))
    return result


def read_env_file(path):
    """Read simple deployed dotenv values without expansion or shell execution."""
    result = {}
    try:
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            result[key] = value
    except (OSError, UnicodeError):
        raise SourceError("environment_file_unreadable") from None
    return result


@dataclass(frozen=True)
class PageCredential:
    page_id: str
    credential_id: str
    fb_user_id: str
    token: str = field(repr=False)

    def safe_context(self):
        return {"page_id": self.page_id, "credential_id": self.credential_id,
                "fb_user_id": self.fb_user_id}

    @property
    def context(self):
        return self.safe_context()


class _Snapshot:
    def __init__(self, connection, schema):
        self.connection, self.schema = connection, schema

    def select(self, sql, params=()):
        if not sql.lstrip().upper().startswith("SELECT ") or ";" in sql:
            raise SourceError("only_single_select_allowed")
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchmany(QUERY_LIMIT + 1)
            if len(rows) > QUERY_LIMIT:
                raise SourceError("source_query_limit_exceeded")
            return [dict(row) for row in rows]
        except SourceError:
            raise
        except Exception:
            raise SourceError("source_query_failed") from None


class ReadOnlyMySQL:
    """At most two process-local manual connections, pinned to the read replica."""
    def __init__(self, config: Mapping[str, str], *, connect_fn=None):
        def value(suffix, default=""):
            for prefix in ("FB_AUTO_MYSQL_", "ADMIN_MAPPING_MYSQL_", "MYSQL_", "DRAMA_DB_"):
                if str(config.get(prefix + suffix, "")).strip():
                    return str(config[prefix + suffix])
            return default
        host, port = value("HOST", "101.32.56.53").strip(), value("PORT", "63350").strip()
        self.schema = value("DATABASE", "kunlunads_dev").strip()
        if host != "101.32.56.53" or port != "63350":
            raise SourceError("read_only_endpoint_required")
        if not _SCHEMA.fullmatch(self.schema):
            raise SourceError("invalid_source_schema")
        self._config = {"host": host, "port": 63350, "user": value("USER"),
                        "password": value("PASSWORD"), "database": self.schema,
                        "charset": "utf8mb4", "autocommit": True,
                        "connect_timeout": 5, "read_timeout": 25, "write_timeout": 5}
        self._connect_fn = connect_fn

    @classmethod
    def from_env_file(cls, path="/root/drama_material_service/.env"):
        return cls(read_env_file(path))

    @contextmanager
    def snapshot(self):
        connection = None
        with _CONNECTIONS:
            try:
                if self._connect_fn is None:
                    import pymysql
                    connection = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **self._config)
                else:
                    connection = self._connect_fn(**self._config)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT @@read_only AS read_only")
                    check = cursor.fetchone()
                    if not check or int(check["read_only"]) != 1:
                        raise SourceError("database_not_read_only")
                    cursor.execute("SET SESSION MAX_EXECUTION_TIME=15000")
                    cursor.execute("SET SESSION time_zone='+08:00'")
                    cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                yield _Snapshot(connection, self.schema)
            except SourceError:
                raise
            except Exception:
                raise SourceError("source_connection_or_snapshot_failed") from None
            finally:
                if connection is not None:
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                    try:
                        connection.close()
                    except Exception:
                        pass


class SourceAdapter:
    def __init__(self, mysql, ai_db_path=DEFAULT_AI_DB, *, groups=ALLOWED_GROUPS):
        groups = tuple(sorted(set(int(item) for item in groups)))
        if groups != ALLOWED_GROUPS:
            raise SourceError("exact_authorized_groups_required")
        self.mysql, self.ai_db_path, self.groups = mysql, str(ai_db_path), groups

    @classmethod
    def from_env(cls, env_paths=None, ai_db_path=DEFAULT_AI_DB):
        values = {}
        for path in (env_paths if env_paths is not None else
                     ["/root/drama_material_service/.env", "/etc/fb-auto-post.env"]):
            values.update(read_env_file(path))
        return cls(ReadOnlyMySQL(values), ai_db_path)

    def _conflicting_logs(self, reader, candidates, start, end):
        """Retain contradictory provenance even when its material is not flagged.

        Candidate discovery is flag-filtered. This separate bounded lookup reads
        all source mappings for those same identifiers before deletion eligibility.
        """
        by_page = {}
        for item in candidates:
            by_page.setdefault(item["page_id"], []).append(item)
        conflicted = set()
        details = []
        local_start, local_end = (value.astimezone(BEIJING).replace(tzinfo=None) for value in (start, end))
        for page_batch in chunks(sorted(by_page)):
            aliases = {value for pid in page_batch for item in by_page[pid]
                       for value in post_aliases(pid, (item["raw_post_id"], item["real_post_id"]))}
            for alias_batch in chunks(sorted(aliases), 250):
                for table in ("ads_facebook_post_log", "ads_post_auto_publish_logs"):
                    automatic = table == "ads_post_auto_publish_logs"
                    index = "created_at" if automatic else "page_id"
                    status = " AND l.status=1" if automatic else ""
                    extra = ",CAST(l.resource_id AS CHAR) AS resource_id" if automatic else ",'' AS resource_id"
                    rows = reader.select(f"""SELECT /*+ MAX_EXECUTION_TIME(15000) */
                        CAST(l.id AS CHAR) AS log_id,CAST(l.page_id AS CHAR) AS page_id,
                        CAST(l.source_id AS CHAR) AS source_row_id,CAST(s.source_id AS CHAR) AS material_id,
                        s.source_type,s.fb_violation,CAST(l.post_id AS CHAR) AS raw_post_id,
                        CAST(l.real_post_id AS CHAR) AS real_post_id{extra}
                        FROM `{reader.schema}`.`{table}` l FORCE INDEX (`{index}`)
                        LEFT JOIN `{reader.schema}`.ads_source s ON s.id=l.source_id
                        WHERE l.page_id IN ({slots(page_batch)}) AND l.created_at>=%s AND l.created_at<%s{status}
                        AND (l.post_id IN ({slots(alias_batch)}) OR l.real_post_id IN ({slots(alias_batch)}))
                        LIMIT {QUERY_LIMIT + 1}""", [*page_batch, local_start, local_end, *alias_batch, *alias_batch])
                    for row in rows:
                        row_aliases = post_aliases(row["page_id"], (row.get("raw_post_id"), row.get("real_post_id")))
                        for candidate in by_page.get(str(row["page_id"]), []):
                            if not row_aliases.intersection(post_aliases(candidate["page_id"], (candidate["raw_post_id"], candidate["real_post_id"]))):
                                continue
                            if (str(row.get("source_type")) != "3" or str(row.get("fb_violation")) != "1"
                                    or str(row.get("material_id")) != candidate["material_id"]
                                    or (automatic and str(row.get("resource_id") or "").strip()
                                        and str(row["resource_id"]).strip() != candidate["material_id"])):
                                key = (candidate["table"], candidate["log_id"])
                                conflicted.add(key)
                                details.append({**candidate, "reason": "conflicting_log_material_identity",
                                                "conflicting_table": table, "conflicting_log_id": str(row["log_id"]),
                                                "conflicting_source_row_id": str(row.get("source_row_id") or ""),
                                                "conflicting_material_id": str(row.get("material_id") or ""),
                                                "conflicting_fb_violation": row.get("fb_violation")})
        unique_details = {str((item["table"], item["log_id"], item["conflicting_table"], item["conflicting_log_id"])): item
                          for item in details}
        return [item for item in candidates if (item["table"], item["log_id"]) not in conflicted], list(unique_details.values())

    def _pages(self, reader, page_id=None):
        restriction = " AND i.page_id=%s" if page_id else ""
        rows = reader.select(f"""SELECT /*+ MAX_EXECUTION_TIME(15000) */ DISTINCT
            CAST(i.page_id AS CHAR) AS page_id,g.id AS group_id
            FROM `{reader.schema}`.ads_facebook_page_group g
            JOIN `{reader.schema}`.ads_facebook_page_group_ins i ON i.group_id=g.id
            JOIN `{reader.schema}`.ads_facebook_page_ins p ON p.page_id=i.page_id
            WHERE g.id IN ({slots(self.groups)}) AND g.is_delete=0
              AND i.deleted_at=0 AND p.status=0{restriction}
            ORDER BY i.page_id,g.id LIMIT {QUERY_LIMIT + 1}""",
            [*self.groups, *([identifier(page_id)] if page_id else [])])
        pages = {}
        for row in rows:
            pid, group = identifier(row["page_id"]), int(row["group_id"])
            if group not in self.groups or (page_id and pid != str(page_id)):
                raise SourceError("scope_query_returned_unexpected_identity")
            pages.setdefault(pid, set()).add(group)
        return {pid: sorted(groups) for pid, groups in pages.items()}

    def _sql_logs(self, reader, pages, start, end):
        candidates, unresolved = [], []
        local_start, local_end = (value.astimezone(BEIJING).replace(tzinfo=None) for value in (start, end))
        for table in ("ads_facebook_post_log", "ads_post_auto_publish_logs"):
            automatic = table == "ads_post_auto_publish_logs"
            extra = ",CAST(l.resource_id AS CHAR) AS resource_id,CAST(l.fb_user_id AS CHAR) AS publisher_fb_user_id" if automatic else ",'' AS resource_id,'' AS publisher_fb_user_id"
            # Auto's page index is not guaranteed; its verified time index is.
            index = " FORCE INDEX (`created_at`)" if automatic else " FORCE INDEX (`page_id`)"
            status = " AND l.status=1" if automatic else ""
            for batch in chunks(sorted(pages)):
                rows = reader.select(f"""SELECT /*+ MAX_EXECUTION_TIME(15000) */
                    CAST(l.id AS CHAR) AS log_id,CAST(l.page_id AS CHAR) AS page_id,
                    CAST(l.source_id AS CHAR) AS source_row_id,s.source_type,
                    CAST(s.source_id AS CHAR) AS material_id,s.fb_violation,
                    CAST(l.post_id AS CHAR) AS raw_post_id,CAST(l.real_post_id AS CHAR) AS real_post_id,
                    l.created_at AS published_log_time{extra}
                    FROM `{reader.schema}`.`{table}` l{index}
                    JOIN `{reader.schema}`.ads_source s ON s.id=l.source_id
                    WHERE l.page_id IN ({slots(batch)}) AND l.created_at>=%s AND l.created_at<%s
                      AND s.source_type=3 AND s.fb_violation=1{status}
                    ORDER BY l.created_at,l.id LIMIT {QUERY_LIMIT + 1}""", [*batch, local_start, local_end])
                for row in rows:
                    base = {"origin": "ordinary_auto" if automatic else "ordinary", "table": table,
                            "log_id": str(row["log_id"]), "page_id": str(row["page_id"]),
                            "source_row_id": str(row["source_row_id"]), "source_type": row["source_type"],
                            "material_id": str(row["material_id"] or "").strip(),
                            "raw_post_id": str(row.get("raw_post_id") or "").strip(),
                            "real_post_id": str(row.get("real_post_id") or "").strip(),
                            "publisher_fb_user_id": str(row.get("publisher_fb_user_id") or ""),
                            "group_ids": pages.get(str(row["page_id"]), []), "fb_violation": row["fb_violation"]}
                    try:
                        if not base["group_ids"] or int(row["source_type"]) != 3 or int(row["fb_violation"]) != 1:
                            raise SourceError("source_scope_or_flag_conflict")
                        identifier(base["material_id"])
                        when = utc(row["published_log_time"], naive_timezone=BEIJING)
                        base["published_at_utc"] = stamp(when)
                        base["time_source"] = table + ".created_at (Asia/Shanghai)"
                        if not start <= when < end:
                            raise SourceError("outside_frozen_window")
                        resource_id = str(row.get("resource_id") or "").strip()
                        if automatic:
                            base["resource_id"] = resource_id
                            if resource_id and resource_id != base["material_id"]:
                                raise SourceError("auto_resource_material_mismatch")
                        candidates.append(base)
                    except SourceError as exc:
                        unresolved.append({**base, "reason": str(exc)})
        return candidates, unresolved

    def _material_rows(self, reader, material_ids):
        result = {}
        for batch in chunks(sorted({identifier(value) for value in material_ids})):
            rows = reader.select(f"""SELECT /*+ MAX_EXECUTION_TIME(15000) */ CAST(id AS CHAR) AS source_row_id,
                CAST(source_id AS CHAR) AS material_id,source_type,fb_violation
                FROM `{reader.schema}`.ads_source WHERE source_type=3 AND source_id IN ({slots(batch)})
                LIMIT {QUERY_LIMIT + 1}""", batch)
            for row in rows:
                result.setdefault(str(row["material_id"]), []).append(row)
        return result

    def _exact_source_rows(self, reader, source_row_ids):
        result = {}
        for batch in chunks(sorted(source_row_ids)):
            rows = reader.select(f"""SELECT /*+ MAX_EXECUTION_TIME(15000) */ CAST(id AS CHAR) AS source_row_id,
                CAST(source_id AS CHAR) AS material_id,source_type,fb_violation
                FROM `{reader.schema}`.ads_source WHERE id IN ({slots(batch)})
                LIMIT {QUERY_LIMIT + 1}""", batch)
            for row in rows:
                result.setdefault(str(row["material_id"]), []).append(row)
        return result

    def _ai_rows(self, pages):
        """Read only lightweight fields, keeping uncertain states out of candidates."""
        try:
            path = Path(self.ai_db_path).resolve()
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            result = []
            for batch in chunks(sorted(pages)):
                placeholders = ",".join("?" for _ in batch)
                auto = connection.execute(f"""SELECT t.id AS log_id,t.page_id,t.material_id,t.status,
                    t.graph_post_id,t.unknown_outcome,t.completed_at_utc AS published_time,
                    l.status AS ledger_status,l.page_id AS ledger_page_id,l.material_id AS ledger_material_id,
                    l.graph_post_id AS ledger_post_id,l.unknown_outcome AS ledger_unknown,
                    (SELECT a.fb_user_id FROM fb_auto_publish_attempt a
                     WHERE a.task_id=t.id AND a.result_kind='accepted' ORDER BY a.id DESC LIMIT 1) AS publisher_fb_user_id
                    FROM fb_auto_task t LEFT JOIN fb_auto_publish_ledger l ON l.task_id=t.id
                    WHERE t.page_id IN ({placeholders}) AND
                      (t.status IN ('published','submitted','unknown') OR t.unknown_outcome=1)
                    ORDER BY t.id LIMIT {QUERY_LIMIT + 1}""", batch).fetchall()
                manual = connection.execute(f"""SELECT t.task_id AS log_id,t.page_id,t.material_id,t.status,
                    t.graph_post_id,t.unknown_outcome,
                    (SELECT MIN(a.created_at_utc) FROM fb_video_attempt a WHERE a.task_id=t.task_id
                     AND a.result_kind='success' AND a.graph_post_id=t.graph_post_id) AS published_time,
                    (SELECT a.fb_user_id FROM fb_video_attempt a WHERE a.task_id=t.task_id
                     AND a.result_kind='success' AND a.graph_post_id=t.graph_post_id ORDER BY a.id LIMIT 1) AS publisher_fb_user_id
                    FROM fb_video_task t WHERE t.page_id IN ({placeholders}) AND
                      (t.status IN ('published','submitted','unknown') OR t.unknown_outcome=1)
                    ORDER BY t.task_id LIMIT {QUERY_LIMIT + 1}""", batch).fetchall()
                if len(auto) > QUERY_LIMIT or len(manual) > QUERY_LIMIT:
                    raise SourceError("ai_query_limit_exceeded")
                result.extend({**dict(row), "origin": "ai_auto", "table": "fb_auto_task"} for row in auto)
                result.extend({**dict(row), "origin": "ai_manual", "table": "fb_video_task"} for row in manual)
            return result
        except SourceError:
            raise
        except (OSError, sqlite3.Error):
            raise SourceError("ai_read_only_scan_failed") from None
        finally:
            if "connection" in locals():
                connection.close()

    def _ai_candidates(self, reader, pages, start, end, *, rows=None):
        rows = self._ai_rows(pages) if rows is None else rows
        materials = self._material_rows(reader, [row["material_id"] for row in rows if _ID.fullmatch(str(row["material_id"]))])
        candidates, unresolved = [], []
        for row in rows:
            mid, pid = str(row["material_id"]), str(row["page_id"])
            flagged = [source for source in materials.get(mid, []) if int(source["fb_violation"]) == 1]
            base = {"origin": row["origin"], "table": row["table"], "log_id": str(row["log_id"]),
                    "page_id": pid, "group_ids": pages[pid], "material_id": mid,
                    "source_row_ids": sorted(str(source["source_row_id"]) for source in flagged),
                    "source_type": 3, "raw_post_id": str(row["graph_post_id"] or ""),
                    "real_post_id": "",
                    "publisher_fb_user_id": str(row.get("publisher_fb_user_id") or ""),
                    "status": row["status"], "fb_violation": 1 if flagged else None}
            # Report uncertain publisher tasks even when not currently violating.
            if row["status"] != "published" or int(row["unknown_outcome"] or 0) != 0:
                unresolved.append({**base, "reason": "ai_publication_unresolved"})
                continue
            if not flagged:
                continue
            try:
                if row["origin"] == "ai_auto" and (
                    row["ledger_status"] != "published" or int(row["ledger_unknown"] or 0) != 0
                    or str(row["ledger_page_id"]) != pid or str(row["ledger_material_id"]) != mid
                    or not row["graph_post_id"] or row["ledger_post_id"] != row["graph_post_id"]
                ):
                    raise SourceError("ai_task_ledger_mismatch")
                when = utc(row["published_time"])
                if not start <= when < end:
                    continue
                base["published_at_utc"] = stamp(when)
                base["time_source"] = ("fb_auto_task.completed_at_utc" if row["origin"] == "ai_auto"
                                       else "fb_video_attempt.success.created_at_utc")
                if not base["raw_post_id"]:
                    raise SourceError("ai_post_id_missing")
                candidates.append(base)
            except SourceError as exc:
                unresolved.append({**base, "reason": str(exc)})
        return candidates, unresolved, {"ai_rows_reviewed": len(rows)}

    def _conflicting_ai(self, candidates, rows):
        """Audit known IDs from all scoped AI tasks, including non-target materials."""
        by_page = {}
        for row in rows:
            by_page.setdefault(str(row["page_id"]), []).append(row)
        eligible, unresolved = [], []
        for candidate in candidates:
            aliases = post_aliases(candidate["page_id"], (candidate["raw_post_id"], candidate["real_post_id"]))
            conflict = None
            for row in by_page.get(candidate["page_id"], []):
                if candidate["table"] == row["table"] and candidate["log_id"] == str(row["log_id"]):
                    continue
                if not aliases.intersection(post_aliases(row["page_id"], [row["graph_post_id"]])):
                    continue
                reason = ""
                if str(row["material_id"]) != candidate["material_id"]:
                    reason = "conflicting_ai_material_identity"
                elif row["status"] != "published" or int(row["unknown_outcome"] or 0) != 0:
                    reason = "ai_publication_unresolved_for_candidate"
                elif row["origin"] == "ai_auto" and (
                    row["ledger_status"] != "published" or int(row["ledger_unknown"] or 0) != 0
                    or str(row["ledger_page_id"]) != str(row["page_id"])
                    or str(row["ledger_material_id"]) != str(row["material_id"])
                    or row["ledger_post_id"] != row["graph_post_id"]
                ):
                    reason = "ai_task_ledger_mismatch_for_candidate"
                if reason:
                    conflict = {**candidate, "reason": reason, "conflicting_table": row["table"],
                                "conflicting_log_id": str(row["log_id"]),
                                "conflicting_material_id": str(row["material_id"]),
                                "conflicting_status": row["status"]}
                    break
            if conflict is None:
                eligible.append(candidate)
            else:
                unresolved.append(conflict)
        return eligible, unresolved

    def freeze(self, start_utc, end_utc):
        start, end = utc(start_utc), utc(end_utc)
        if start >= end or end - start > timedelta(days=90):
            raise SourceError("invalid_90_day_window")
        captured = stamp(datetime.now(UTC))
        with self.mysql.snapshot() as reader:
            pages = self._pages(reader)
            candidates, unresolved = self._sql_logs(reader, pages, start, end)
            ai_rows = self._ai_rows(pages)
            ai, ai_unresolved, stats = self._ai_candidates(reader, pages, start, end, rows=ai_rows)
            candidates, conflicts = self._conflicting_logs(reader, candidates + ai, start, end)
            candidates, ai_conflicts = self._conflicting_ai(candidates, ai_rows)
        return {"captured_at_utc": captured, "window_start_utc": stamp(start), "window_end_utc": stamp(end),
                "group_ids": list(self.groups),
                "pages": [{"page_id": pid, "group_ids": groups} for pid, groups in sorted(pages.items())],
                "candidates": candidates, "unresolved": unresolved + conflicts + ai_unresolved + ai_conflicts, "statistics": stats}

    def precheck(self, page_id, material_ids, source_row_ids=None):
        page_id = identifier(page_id)
        wanted = {identifier(value) for value in material_ids}
        exact_rows = {identifier(value) for value in (source_row_ids or [])}
        with self.mysql.snapshot() as reader:
            pages = self._pages(reader, page_id)
            materials = (self._exact_source_rows(reader, exact_rows) if exact_rows
                         else self._material_rows(reader, wanted))
        flagged = {mid for mid, rows in materials.items() if any(str(row["source_type"]) == "3" and str(row["fb_violation"]) == "1" for row in rows)}
        current = {str(row["source_row_id"]): row for rows in materials.values() for row in rows}
        row_match = all(rid in current and str(current[rid]["material_id"]) in wanted
                        and str(current[rid]["source_type"]) == "3"
                        and str(current[rid]["fb_violation"]) == "1" for rid in exact_rows)
        reason = ("page_outside_current_scope" if page_id not in pages else
                  "no_material_identity" if not wanted else
                  "material_no_longer_violating" if not wanted <= flagged else
                  "source_row_changed" if not row_match else "")
        return {"ok": not reason, "eligible": not reason, "reason": reason, "page_id": page_id,
                "group_ids": pages.get(page_id, []), "violating_material_ids": sorted(flagged),
                "checked_at_utc": stamp(datetime.now(UTC))}

    def credentials(self, page_id, preferred_fb_user_id=None):
        page_id = identifier(page_id)
        with self.mysql.snapshot() as reader:
            rows = reader.select(f"""SELECT /*+ MAX_EXECUTION_TIME(15000) */
                CAST(id AS CHAR) AS credential_id,CAST(page_id AS CHAR) AS page_id,
                CAST(fb_user_id AS CHAR) AS fb_user_id,page_access_token
                FROM `{reader.schema}`.ads_facebook_page_ins
                WHERE page_id=%s AND status=0 AND TRIM(page_access_token)<>''
                ORDER BY id LIMIT 101""", [page_id])
        if len(rows) > 100:
            raise SourceError("credential_candidate_limit_exceeded")
        rows.sort(key=lambda row: (str(row.get("fb_user_id") or "") != str(preferred_fb_user_id or ""), str(row["credential_id"])))
        result, seen = [], set()
        for row in rows:
            if str(row["page_id"]) != page_id:
                raise SourceError("credential_page_mismatch")
            token = str(row.get("page_access_token") or "").strip()
            if token and token not in seen:
                seen.add(token)
                result.append(PageCredential(page_id, str(row["credential_id"]), str(row.get("fb_user_id") or ""), token))
        return result
