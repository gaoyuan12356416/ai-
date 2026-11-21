"""Bounded FIFO-gated replica reads and immutable Page portrait snapshots."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from functools import lru_cache
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from .runtime import database_error, log


class SourceError(Exception):
    def __init__(self, code, *, required_records=None):
        super().__init__(code)
        self.code = code
        self.required_records = required_records


def literal(value):
    return "0x" + str(value).encode("utf-8").hex() if str(value) else "''"


class Reader:
    """The normal automation FIFO owns concurrency; never bypass it."""

    def __init__(self, env):
        self.env = env
        if (env.get("ADMIN_MAPPING_MYSQL_HOST"), str(env.get("ADMIN_MAPPING_MYSQL_PORT"))) != ("101.32.56.53", "63350"):
            raise SourceError("invalid_reader_endpoint")

    def query(self, statements, *, optional_statements=()):
        """Use one gated connection, retaining completed required reads if labels fail.

        The checkpoint is emitted only after every required statement succeeds.
        mysql stops on the first error, so an optional failure cannot disguise a
        failed Page/metadata query as an empty or successful result.
        """
        env = dict(os.environ)
        env.pop("SQL_GATE_BYPASS", None)
        env.update(MYSQL_PWD=self.env["ADMIN_MAPPING_MYSQL_PASSWORD"],
                   SQL_GATE_JOB_NAME="fb-page-assignment", SQL_GATE_WAIT_TIMEOUT_SECONDS="8",
                   SQL_GATE_EXEC_TIMEOUT_SECONDS="30", SQL_GATE_RETRY_DELAYS_SECONDS="")
        boundary = "fb_page_assignment_required_complete"
        queries = ";\n".join(statements)
        if optional_statements:
            queries += ";\nSELECT '" + boundary + "';\n" + ";\n".join(optional_statements)
        # The mysql CLI normally dispatches every semicolon separately. A
        # dedicated delimiter sends this fixed read-only batch in one packet,
        # avoiding a network round trip for each SET/SELECT/checkpoint.
        sql = ("delimiter //\n"
               "SET SESSION time_zone='+00:00', MAX_EXECUTION_TIME=8000, tx_isolation='REPEATABLE-READ'; "
               "START TRANSACTION READ ONLY; SELECT @@read_only;\n" +
               queries + ";\nROLLBACK//\n")
        try:
            for attempt in range(2):
                started = time.monotonic()
                result = subprocess.run([
                    "/usr/bin/mysql", "-h", "101.32.56.53", "-P", "63350", "-u",
                    self.env["ADMIN_MAPPING_MYSQL_USER"], "--connect-timeout=3", "--skip-reconnect",
                    "--default-character-set=utf8mb4", "--compress", "--unbuffered", "-N", "-B", "-r"],
                    input=sql, env=env, text=True, encoding="utf-8", capture_output=True, timeout=42)
                codes = re.findall(r"ERROR (\d+)", result.stderr)
                if result.returncode:
                    log("database_failure", role="reader", port=63350, attempt=attempt + 1,
                        phase="initial_session" if not result.stdout.strip() else "read",
                        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                        **database_error(result.stderr))
                # An observed upstream handshake failure produced no results.
                # Retry once through the same FIFO gate, never SQL failures,
                # partially read results, optional failures or execution timeouts.
                if (attempt == 0 and result.returncode and not result.stdout.strip()
                        and any(code in {"2003", "2013"} for code in codes)):
                    log("source_connection_retry", error_code="_".join(codes))
                    continue
                break
            stdout, stderr = result.stdout, result.stderr
            error = "source_query_failed" if result.returncode else ""
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, error = exc.stdout or "", exc.stderr or "", "source_query_timeout"
        except OSError:
            raise SourceError("source_connection_failed") from None
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        codes = re.findall(r"ERROR (\d+)", stderr)
        if error and codes:
            error += "_" + "_".join(codes)
        lines = stdout.splitlines()
        if not lines or lines.pop(0) != "1":
            raise SourceError(error or "source_query_failed")
        checkpoint = lines.index(boundary) if optional_statements and boundary in lines else None
        if error:
            required = self._decode(lines[:checkpoint]) if checkpoint is not None else None
            raise SourceError(error, required_records=required)
        if optional_statements and checkpoint is None:
            raise SourceError("source_invalid_response")
        return self._decode([line for line in lines if line != boundary])

    @staticmethod
    def _decode(lines):
        try:
            return [json.loads(bytes.fromhex(line).decode("utf-8")) for line in lines if line.strip()]
        except (ValueError, TypeError):
            raise SourceError("source_invalid_response") from None


class Sources:
    def __init__(self, env, reader=None, classifier=None, normalize_language=None):
        self.env = env
        self.reader = reader or Reader(env)
        if classifier is None or normalize_language is None:
            from .runtime import shared
            shared(env)
            from features.x_tag_revenue.taxonomy import classification_index
            from features.x_tag_revenue.core import language
            classifier, normalize_language = classification_index, language
        self.classifier = classifier
        self.language = normalize_language

    def load(self, ids):
        keys = ",".join(literal(cid) for cid in sorted(set(ids)))
        required = [
            """SELECT HEX(JSON_OBJECT('kind','page','member_id',i.id,'page_id',TRIM(i.page_id),
                'language',i.language,'source_id',p.id,'status',p.status,'forbidden_status',p.forbidden_status,
                'is_permission',p.is_permission,'active_ad_count',p.active_ad_count,'fan_ccount',p.fan_ccount,
                'updated_at',CAST(p.updated_at AS CHAR)))
                FROM kunlunads_dev.ads_facebook_page_group g
                JOIN kunlunads_dev.ads_facebook_page_group_ins i ON i.group_id=g.id AND i.deleted_at=0
                LEFT JOIN kunlunads_dev.ads_facebook_page_ins p ON p.page_id=i.page_id
                WHERE g.id=62 AND g.app_id=1479 AND g.is_delete=0 AND TRIM(i.page_id)<>'' LIMIT 10001""",
            "SELECT DISTINCT HEX(JSON_OBJECT('kind','info','content_id',content_id,'app_id',app_id,"
            "'language',language,'series_code',series_code,'resource_type',resource_type)) "
            "FROM kunlunads_dev.ads_drama_info WHERE app_id=1479 AND content_id IN (" + keys + ") LIMIT 20001"
        ]
        optional = [
            "SELECT DISTINCT HEX(JSON_OBJECT('kind','resource','content_id',content_id,'app_id',app_id,'language',language,"
            "'series_code',series_code,'labels',labels)) FROM kunlunads_dev.ads_drama_resource "
            "WHERE app_id=1479 AND content_id IN (" + keys + ") LIMIT 20001"
        ]
        warnings = []
        try:
            records = self.reader.query(required, optional_statements=optional)
        except SourceError as exc:
            if exc.required_records is None:
                raise
            records = exc.required_records
            warnings.append("drama_labels_" + exc.code)
        infos = [r for r in records if r.get("kind") == "info"]
        page_rows = [r for r in records if r.get("kind") == "page"]
        resources = [r for r in records if r.get("kind") == "resource"]
        if len(infos) > 20000 or len(page_rows) > 10000:
            raise SourceError("source_row_limit")
        if len(resources) > 20000:
            resources = []
            warnings.append("drama_labels_source_label_row_limit")
        # SQL's default collation can match differently-cased IDs. Keep the
        # exact user-supplied content ID as the final identity boundary.
        wanted = set(ids)
        infos = [r for r in infos if str(r.get("content_id")) in wanted]
        resources = [r for r in resources if str(r.get("content_id")) in wanted]
        classifications = self.classifier(resources, infos)
        grouped = defaultdict(list)
        for row in infos:
            grouped[str(row["content_id"])].append(row)
        dramas = {}
        for cid in wanted:
            rows = grouped[cid]
            languages = {self.language(r.get("language")) for r in rows}
            known = languages - {"", "unknown", "__unknown__"}
            language = next(iter(known)) if len(known) == 1 and len(languages) == 1 else ""
            classification = classifications.get((cid, language), {}) if language else {}
            dramas[cid] = dict(exists=bool(rows), language=language,
                               audience=classification.get("audience", "__unknown__"),
                               themes=classification.get("themes", []), classification=classification,
                               warnings=(["drama_language_conflict"] if len(known) > 1 else
                                         ["drama_language_missing"] if rows and not language else []))
        grouped_pages = defaultdict(list)
        for row in page_rows:
            grouped_pages[str(row["page_id"])].append(row)
        pages = []
        for pid, rows in grouped_pages.items():
            if not re.fullmatch(r"[1-9][0-9]{0,31}", pid):
                warnings.append("page_id_invalid")
                continue
            row = dict(max(rows, key=lambda r: (str(r.get("updated_at") or ""), int(r.get("source_id") or 0))))
            languages = {self.language(r.get("language")) if str(r.get("language") or "").strip() else "" for r in rows}
            row["language"] = next(iter(languages)) if len(languages) == 1 else ""
            if len(languages) != 1 or row["language"] in {"unknown", "__unknown__"}:
                row["status"] = 1
                row["source_error"] = "page_language_invalid_or_conflicting"
                warnings.append("page_language_invalid_or_conflicting")
            pages.append(row)
        return dict(dramas=dramas, pages=pages, warnings=sorted(set(warnings)),
                    source_read_at=datetime.now(timezone.utc).isoformat())


@lru_cache(maxsize=8)
def mysql_profiles(reader, version, ids):
    table = "ads_ai.fb_page_revenue_profile"
    scope = " FROM " + table + " WHERE profile_version=" + literal(version)
    queries = ["SELECT HEX(meta_json)" + scope + " LIMIT 1"]
    for start in range(0, len(ids), 100):
        batch = ",".join(literal(pid) for pid in ids[start:start + 100])
        queries.append("SELECT HEX(JSON_OBJECT('id',page_id,'profile',detail_json))" + scope
                       + " AND page_id IN (" + batch + ")")
    records = reader.query(queries)
    if not records or records[0].get("version") != version:
        raise ValueError("snapshot_version_unavailable")
    found, warnings = {}, []
    for row in records[1:]:
        pid = str(row["id"])
        try:
            profile = json.loads(row["profile"])
            if not isinstance(profile, dict) or str(profile.get("page_id")) != pid:
                raise ValueError("invalid_profile")
            # Selection only uses audience/theme keys, matchability and shares.
            for audience in profile.get("audiences") or []:
                for theme in audience.get("children") or []:
                    theme.pop("dramas", None)
            found[pid] = {key: profile.get(key) for key in ("page_id", "status", "audiences")}
        except (ValueError, TypeError, AttributeError):
            warnings.append("profile_invalid:" + pid)
    return found, records[0], warnings


def load_profiles(ids, root="/mnt/data-disk/fb-page-portraits", *, reader=None):
    root = Path(root).resolve()
    try:
        current = json.loads((root / "current.json").read_text(encoding="utf-8"))
        if current.get("backend") == "mysql":
            if (reader is None or current.get("database") != "ads_ai.fb_page_revenue_profile"
                    or not re.fullmatch(r"[0-9]{8}T[0-9]{12}Z", current["version"])):
                raise ValueError("invalid_mysql_snapshot")
            return mysql_profiles(reader, current["version"], tuple(sorted(set(ids))))
        path = (root / current["database"]).resolve()
        if path.parent != root or not path.is_file():
            raise ValueError("invalid_snapshot_path")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            meta = json.loads(db.execute("SELECT payload FROM metadata").fetchone()[0])
            if meta["version"] != current["version"]:
                raise ValueError("snapshot_version_mismatch")
            found = {}
            warnings = []
            for start in range(0, len(ids), 100):
                batch = ids[start:start + 100]
                for pid, raw in db.execute("SELECT page_id,tree FROM profiles WHERE page_id IN (" +
                                           ",".join("?" for _ in batch) + ")", batch):
                    try:
                        profile = json.loads(raw)
                        if not isinstance(profile, dict) or str(profile.get("page_id")) != str(pid):
                            raise ValueError("invalid_profile")
                        found[pid] = profile
                    except (ValueError, TypeError):
                        warnings.append("profile_invalid:" + str(pid))
            return found, meta, warnings
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, SourceError):
        return {}, {}, ["profile_snapshot_unavailable"]
