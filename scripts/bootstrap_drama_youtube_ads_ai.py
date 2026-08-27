#!/usr/bin/env python3
"""Create only missing owned ads_ai tables; never alter or copy legacy data.

Dry-run is database/file read-only on 63350. Production apply is pinned to a
clean Git commit, fixed reviewed SQL, and fresh data-disk discovery/rehearsal
evidence. Rehearsal writes only to a separately inspected loopback MySQL 5.7.
All reports go to stdout; the operator preserves them in private evidence files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.unified_youtube import TABLE_BY_KIND, read_secure_owned_file  # noqa: E402
from features.drama_synthesis.unified_youtube_rpc import (  # noqa: E402
    ACCOUNT_HOST, LedgerRPCError, SCHEMA, TABLE_OWNERSHIP_COMMENT,
    UnifiedYouTubeLedger, inspect_owned_tables,
)

HOST = "101.32.56.53"
READER_PORT = 63350
WRITER_PORT = 63353
ADMIN_USER = "ads_aius"
CLUSTER_ID = "cynosdbmysql-5kxxsre7"
REHEARSAL_USER = "drama_ads_ai_rehearsal"
REHEARSAL_PORT = 23358
REHEARSAL_IMAGE_DIGEST = "mysql@sha256:dab0a802b44617303694fb17d166501de279c3031ddeb28c56ecf7fcab5ef0da"
REHEARSAL_IMAGE_ID = "sha256:5107333e08a87b836d48ff7528b1e84b9c86781cc9f1748bbc1b8c42a870d933"
SQL_PATH = ROOT / "deploy/drama-youtube-ads-ai-v2.sql"
DDL_SHA256 = "08efc2e9d7e7bb52eb9bf041e9133acb214ca6dc8b8c7d86cb73d6d80ee8be38"
INSPECTION_CONTRACT = "drama-youtube-ads-ai-inspection-v2"
REHEARSAL_CONTRACT = "drama-youtube-ads-ai-fresh-rehearsal-v2"
EVIDENCE_CONTRACT = "drama-youtube-ads-ai-bootstrap-evidence-v2"
REHEARSAL_CHECKS = frozenset({
    "fresh_three_table_absence", "create_three_tables", "compatible_rerun_noop",
    "records_preserved", "full_payload_roundtrip", "immutable_conflicts_rejected",
    "out_of_order_allowed", "no_trigger_or_fk", "only_owned_ads_ai_tables",
})


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fresh(value: Any) -> None:
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        raise RuntimeError("bootstrap evidence timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise RuntimeError("bootstrap evidence timestamp is invalid") from None
    age = datetime.now(timezone.utc) - parsed
    if not -timedelta(minutes=5) <= age <= timedelta(hours=4):
        raise RuntimeError("bootstrap evidence is stale")


def _private_data_path(path_text: str) -> Path:
    path = Path(path_text)
    mount = Path("/mnt/data-disk")
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise RuntimeError("bootstrap evidence path is unsafe")
    try:
        path.relative_to(mount)
    except ValueError:
        raise RuntimeError("bootstrap evidence must be on the verified data disk") from None
    if os.name != "posix" or not mount.is_mount() or mount.stat().st_dev == Path("/").stat().st_dev:
        raise RuntimeError("bootstrap data disk is not mounted independently")
    metadata = path.stat()
    if metadata.st_dev != mount.stat().st_dev:
        raise RuntimeError("bootstrap evidence is on the wrong device")
    parent = path.parent.stat()
    if stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != os.geteuid():
        raise RuntimeError("bootstrap evidence directory owner or mode is unsafe")
    return path


def _load_private_json(path_text: str) -> tuple[Mapping[str, Any], str]:
    _private_data_path(path_text)
    try:
        raw = read_secure_owned_file(path_text, max_bytes=65536)
        value = json.loads(raw.decode("utf-8"))
    except (RuntimeError, UnicodeDecodeError, ValueError):
        raise RuntimeError("bootstrap evidence file is invalid") from None
    if not isinstance(value, Mapping):
        raise RuntimeError("bootstrap evidence file is invalid")
    return value, _sha(raw)


def verify_candidate(candidate_git_sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_git_sha):
        raise RuntimeError("exact candidate Git SHA is required")
    def git(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True, timeout=15)
        return result.stdout.strip()
    if git("rev-parse", "HEAD") != candidate_git_sha or git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("bootstrap requires the pinned clean Git candidate")


def load_reviewed_sql() -> dict[str, str]:
    raw = SQL_PATH.read_bytes().replace(b"\r\n", b"\n")
    if _sha(raw) != DDL_SHA256:
        raise RuntimeError("reviewed bootstrap SQL hash is invalid")
    text = "\n".join(line for line in raw.decode("utf-8").splitlines() if not line.startswith("--"))
    statements = [part.strip() for part in text.split(";") if part.strip()]
    if len(statements) != 3:
        raise RuntimeError("reviewed bootstrap SQL statement count is invalid")
    result = {}
    for statement in statements:
        match = re.match(r"CREATE TABLE ads_ai\.(ads_youtube_[a-z_]+) \(", statement)
        if not match or match.group(1) not in TABLE_BY_KIND.values() or match.group(1) in result:
            raise RuntimeError("reviewed bootstrap SQL target is invalid")
        result[match.group(1)] = statement
    return result


def load_admin_credential_file(path_text: str, *, apply: bool = False, rehearsal: bool = False) -> Mapping[str, Any]:
    try:
        value = json.loads(read_secure_owned_file(path_text, max_bytes=8192).decode("utf-8"))
    except (RuntimeError, UnicodeDecodeError, ValueError):
        raise RuntimeError("bootstrap credential file is invalid") from None
    if not isinstance(value, Mapping) or set(value) != {"host", "port", "user", "password", "database"}:
        raise RuntimeError("bootstrap credential file is invalid")
    target = ("127.0.0.1", REHEARSAL_PORT, REHEARSAL_USER) if rehearsal else (HOST, WRITER_PORT if apply else READER_PORT, ADMIN_USER)
    if (type(value.get("port")) is not int or (value.get("host"), value["port"], value.get("user")) != target
            or value.get("database") != SCHEMA):
        raise RuntimeError("bootstrap credential target is invalid")
    password = value.get("password")
    if type(password) is not str or not 1 <= len(password) <= 1024 or any(ord(char) < 32 for char in password):
        raise RuntimeError("bootstrap credential file is invalid")
    return dict(value)


def _connect(config: Mapping[str, Any], *, apply: bool = False, rehearsal: bool = False):
    target = ("127.0.0.1", REHEARSAL_PORT, REHEARSAL_USER) if rehearsal else (HOST, WRITER_PORT if apply else READER_PORT, ADMIN_USER)
    if (set(config) != {"host", "port", "user", "password", "database"}
            or type(config.get("port")) is not int
            or (config.get("host"), config.get("port"), config.get("user")) != target or config.get("database") != SCHEMA):
        raise RuntimeError("bootstrap database target is invalid")
    return pymysql.connect(host=config["host"], port=config["port"], user=config["user"],
                           password=config["password"], database=config["database"],
                           charset="utf8mb4", autocommit=True, connect_timeout=5,
                           read_timeout=30, write_timeout=30, cursorclass=pymysql.cursors.DictCursor)


def _validate_admin(cursor: Any, *, writable: bool, rehearsal: bool = False) -> str:
    cursor.execute("SELECT DATABASE() AS database_name,CURRENT_USER() AS account_name,@@read_only AS read_only")
    identity = cursor.fetchone()
    account = "%s@%%" % REHEARSAL_USER if rehearsal else "%s@%s" % (ADMIN_USER, ACCOUNT_HOST)
    if (not isinstance(identity, Mapping) or identity.get("database_name") != SCHEMA
            or identity.get("account_name") != account or type(identity.get("read_only")) is not int
            or identity["read_only"] not in ({0} if writable else {0, 1})):
        raise RuntimeError("bootstrap database identity is invalid")
    cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
    grants = []
    for row in cursor.fetchall():
        if not isinstance(row, Mapping) or len(row) != 1:
            raise RuntimeError("bootstrap admin grants are invalid")
        grants.append(re.sub(r"\s+", " ", str(next(iter(row.values()))).strip()))
    # The exact ads_ai ALL grant makes table/trigger metadata visible. No grants
    # are installed or changed here, and this admin never serves runtime RPC.
    user, host = account.split("@", 1)
    # MySQL 5.7's official-image initialization escapes the schema underscore
    # in SHOW GRANTS. Accept only that exact single escape, not a general
    # unescape operation that could turn wildcards/other schemas into ads_ai.
    schema_pattern = r"(?:ads_ai|`ads_ai`|'ads_ai'|`ads\\_ai`)"
    pattern = (r"GRANT ALL PRIVILEGES ON " + schema_pattern + r"\.\* TO [`']?" + re.escape(user)
               + r"[`']?@[`']?" + re.escape(host) + r"[`']?(?: WITH GRANT OPTION)?")
    if sum(bool(re.fullmatch(pattern, grant, re.IGNORECASE)) for grant in grants) != 1:
        raise RuntimeError("bootstrap admin lacks exact ads_ai metadata visibility")
    return account


def _run_bootstrap(cursor: Any, *, apply: bool) -> Mapping[str, Any]:
    statements = load_reviewed_sql()
    state = inspect_owned_tables(cursor, allow_missing=True, inspect_triggers=True)
    missing = [table for table in TABLE_BY_KIND.values() if state[table] == "missing"]
    created = []
    if apply:
        for table in missing:
            # No IF NOT EXISTS: a concurrent creator or partial failure stops
            # without rollback DDL, and never alters/deletes the other objects.
            cursor.execute(statements[table])
            created.append(table)
        state = inspect_owned_tables(cursor, inspect_triggers=True)
    return {"table_states": state, "missing_tables": missing, "created_tables": created,
            "complete": all(value == "compatible" for value in state.values())}


def load_apply_evidence(path_text: str, *, candidate_git_sha: str) -> Mapping[str, Any]:
    value, digest = _load_private_json(path_text)
    keys = {"contract", "candidate_git_sha", "ddl_sha256", "discovery_file", "discovery_sha256", "rehearsal_file", "rehearsal_sha256"}
    if (set(value) != keys or value.get("contract") != EVIDENCE_CONTRACT
            or value.get("candidate_git_sha") != candidate_git_sha or value.get("ddl_sha256") != DDL_SHA256):
        raise RuntimeError("bootstrap evidence contract is invalid")
    discovery, discovery_sha = _load_private_json(str(value.get("discovery_file") or ""))
    rehearsal, rehearsal_sha = _load_private_json(str(value.get("rehearsal_file") or ""))
    if discovery_sha != value["discovery_sha256"] or rehearsal_sha != value["rehearsal_sha256"]:
        raise RuntimeError("bootstrap evidence artifact hash is invalid")
    for report in (discovery, rehearsal):
        if (report.get("ok") is not True or report.get("schema") != SCHEMA
                or report.get("candidate_git_sha") != candidate_git_sha or report.get("ddl_sha256") != DDL_SHA256):
            raise RuntimeError("bootstrap evidence candidate or schema mismatch")
        _fresh(report.get("observed_at_utc"))
    if (discovery.get("contract") != INSPECTION_CONTRACT or discovery.get("mode") != "dry-run"
            or discovery.get("host") != HOST or discovery.get("port") != READER_PORT
            or discovery.get("admin_identity") != "%s@%s" % (ADMIN_USER, ACCOUNT_HOST)
            or set(discovery.get("table_states", {})) != set(TABLE_BY_KIND.values())
            or any(state not in {"missing", "compatible"} for state in discovery["table_states"].values())
            or discovery.get("created_tables") != [] or discovery.get("admin_trigger_check") is not True):
        raise RuntimeError("bootstrap discovery evidence is invalid")
    if (rehearsal.get("contract") != REHEARSAL_CONTRACT or rehearsal.get("host") != "127.0.0.1"
            or rehearsal.get("port") != REHEARSAL_PORT or rehearsal.get("engine_version") != "5.7.44"
            or not re.fullmatch(r"[0-9a-f]{16}", str(rehearsal.get("context") or ""))
            or rehearsal.get("checks") != {key: True for key in REHEARSAL_CHECKS}
            or any(value is not True for value in rehearsal.get("checks", {}).values())
            or rehearsal.get("runtime_identity_simulated") is not True):
        raise RuntimeError("bootstrap fresh-table rehearsal evidence is invalid")
    return {"evidence_sha256": digest, "discovery_sha256": discovery_sha, "rehearsal_sha256": rehearsal_sha}


def bootstrap(credential_file: str, *, apply: bool = False, candidate_git_sha: str = "", evidence_file: str = "") -> Mapping[str, Any]:
    if apply and (not candidate_git_sha or not evidence_file):
        raise RuntimeError("production apply requires exact candidate and new evidence")
    if not apply and evidence_file:
        raise RuntimeError("apply evidence is not accepted for a dry-run")
    if candidate_git_sha:
        verify_candidate(candidate_git_sha)
    evidence = load_apply_evidence(evidence_file, candidate_git_sha=candidate_git_sha) if apply else {}
    config = load_admin_credential_file(credential_file, apply=apply)
    connection = _connect(config, apply=apply)
    try:
        with connection.cursor() as cursor:
            account = _validate_admin(cursor, writable=apply)
            result = _run_bootstrap(cursor, apply=apply)
        return {"ok": True, "contract": INSPECTION_CONTRACT, "mode": "apply" if apply else "dry-run",
                "cluster_id": CLUSTER_ID, "schema": SCHEMA, "host": HOST, "port": config["port"],
                "admin_identity": account, "admin_trigger_check": True,
                "observed_at_utc": _now(), "candidate_git_sha": candidate_git_sha,
                "ddl_sha256": DDL_SHA256, **result, **evidence}
    finally:
        connection.close()


def _validate_rehearsal_data_dir(path_text: str) -> None:
    """Prove the bind source is a real directory on the independent data disk."""
    path = Path(path_text)
    mount = Path("/mnt/data-disk")
    if os.name != "posix" or not path.is_absolute() or path == mount:
        raise RuntimeError("rehearsal data directory is invalid")
    try:
        path.relative_to(mount)
        if path.resolve(strict=True) != path or mount.resolve(strict=True) != mount:
            raise RuntimeError("rehearsal data directory contains a symlink")
        mount_metadata = mount.lstat()
        if (not stat.S_ISDIR(mount_metadata.st_mode) or not mount.is_mount()
                or mount_metadata.st_dev == Path("/").stat().st_dev):
            raise RuntimeError("rehearsal data disk is not independently mounted")
        # lstat every path component, not only the final directory. Ownership
        # of the mysql-owned data leaf is not confused with the private root.
        for component in (*reversed(path.parents), path):
            metadata = component.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError("rehearsal data directory contains a non-directory or symlink")
            if (component == mount or mount in component.parents) and metadata.st_dev != mount_metadata.st_dev:
                raise RuntimeError("rehearsal data directory is on another device")
    except (OSError, ValueError):
        raise RuntimeError("rehearsal data directory is unavailable or unsafe") from None
    # st_dev alone cannot detect a same-device nested bind mount pointing at
    # old data. The actual covering mount must be the verified data-disk root.
    result = subprocess.run(["findmnt", "--json", "--target", str(path), "--output", "TARGET,SOURCE"],
                            check=True, capture_output=True, text=True, timeout=15)
    try:
        filesystems = json.loads(result.stdout).get("filesystems")
    except (AttributeError, ValueError):
        raise RuntimeError("rehearsal data directory mount proof is invalid") from None
    if (not isinstance(filesystems, list) or len(filesystems) != 1
            or not isinstance(filesystems[0], Mapping) or filesystems[0].get("target") != str(mount)
            or not str(filesystems[0].get("source") or "")):
        raise RuntimeError("rehearsal data directory has an unexpected covering mount")


def _inspect_rehearsal_container(context: str) -> Mapping[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{16}", context):
        raise RuntimeError("rehearsal context is invalid")
    name = "drama-youtube-ads-ai-rehearsal-" + context
    result = subprocess.run(["docker", "inspect", name], capture_output=True, check=True, text=True, timeout=15)
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 1:
        raise RuntimeError("rehearsal container identity is invalid")
    value = values[0]
    expected_data = "/mnt/data-disk/" + name + "/data"
    mounts = [mount for mount in value.get("Mounts", []) if mount.get("Destination") == "/var/lib/mysql"]
    if (value.get("Name") != "/" + name or value.get("State", {}).get("Running") is not True
            or value.get("Config", {}).get("Image") not in {REHEARSAL_IMAGE_DIGEST, "mysql:5.7.44"}
            or value.get("Image") != REHEARSAL_IMAGE_ID
            or value.get("Config", {}).get("Labels", {}).get("drama_youtube_ads_ai_rehearsal") != context
            or value.get("HostConfig", {}).get("Privileged") is not False
            or value.get("HostConfig", {}).get("NetworkMode") == "host"
            or value.get("NetworkSettings", {}).get("Ports", {}).get("3306/tcp") != [{"HostIp": "127.0.0.1", "HostPort": str(REHEARSAL_PORT)}]
            or len(mounts) != 1 or mounts[0].get("Type") != "bind"
            or mounts[0].get("Source") != expected_data or mounts[0].get("RW") is not True):
        raise RuntimeError("rehearsal container isolation is invalid")
    _validate_rehearsal_data_dir(expected_data)
    return {"container_name": name, "container_id": str(value.get("Id") or ""), "data_dir": expected_data,
            "container_image_reference": str(value["Config"]["Image"]), "container_image_id": value["Image"]}


def rehearse(credential_file: str, *, candidate_git_sha: str, context: str) -> Mapping[str, Any]:
    verify_candidate(candidate_git_sha)
    container = _inspect_rehearsal_container(context)
    expected_credential = "/mnt/data-disk/drama-youtube-ads-ai-rehearsal-" + context + "/admin-db.json"
    if credential_file != expected_credential:
        raise RuntimeError("rehearsal credential context mismatch")
    _private_data_path(credential_file)
    config = load_admin_credential_file(credential_file, rehearsal=True)
    def connect():
        return _connect(config, rehearsal=True)
    connection = connect()
    try:
        with connection.cursor() as cursor:
            _validate_admin(cursor, writable=True, rehearsal=True)
            cursor.execute("SELECT VERSION() AS version")
            if cursor.fetchone().get("version") != "5.7.44":
                raise RuntimeError("rehearsal requires the pinned MySQL version")
            initial = _run_bootstrap(cursor, apply=False)
            if initial["table_states"] != {table: "missing" for table in TABLE_BY_KIND.values()}:
                raise RuntimeError("rehearsal requires three fresh absent tables; existing state is preserved")
            applied = _run_bootstrap(cursor, apply=True)
            if set(applied["created_tables"]) != set(TABLE_BY_KIND.values()):
                raise RuntimeError("fresh-table rehearsal create failed")
    finally:
        connection.close()

    class LoopbackRehearsalLedger(UnifiedYouTubeLedger):
        def _preflight(self, cursor: Any) -> Mapping[str, Any]:
            # Only this isolated test substitutes admin identity. The report
            # explicitly excludes production writer-identity/grant acceptance.
            _validate_admin(cursor, writable=True, rehearsal=True)
            inspect_owned_tables(cursor, inspect_triggers=True)
            return {"runtime_identity_simulated": True}

    ledger = LoopbackRehearsalLedger(connect)
    video = {
        "publish_id": 2147483000, "video_id": "adsai_rehearsal1", "app_id": 1479,
        "channel_local_id": 1, "operator_user_id": "rehearsal_892fd2e8", "job_id": "a" * 32,
        "content_id": "剧集😀-rehearsal", "source_kind": "concat_video",
        "source_url": "https://example.test/" + "a" * 3500 + ".mp4",
        "title": "独立新表😀", "description_rendered": "é" * 2000 + "🙂" * 200 + "a" * 200,
        "privacy_status": "public", "published_at_utc": _now(),
    }
    comment = {key: video[key] for key in ("publish_id", "video_id", "channel_local_id", "operator_user_id", "published_at_utc")}
    comment.update(comment_id="adsai_rehearsal_comment1", comment_text="留言😀" * 250)
    facts = [("publish_log", str(video["publish_id"]), video), ("comment", comment["comment_id"], comment), ("video", video["video_id"], video)]
    for kind, external, payload in facts:
        table = TABLE_BY_KIND[kind]
        if ledger.execute("insert", table, external, payload) != {"idempotent_success": True, "reused": False}:
            raise RuntimeError("rehearsal initial insert failed")
        if ledger.execute("update", table, external, payload) != {"idempotent_success": True, "reused": True}:
            raise RuntimeError("rehearsal idempotent replay failed")
        changed = dict(payload)
        field = "comment_text" if kind == "comment" else "description_rendered"
        changed[field] = changed[field][:-1] + "b"
        try:
            ledger.execute("insert", table, external, changed)
        except LedgerRPCError as exc:
            if exc.code != "youtube_sync_identity_conflict":
                raise
        else:
            raise RuntimeError("rehearsal immutable conflict was accepted")
    connection = connect()
    try:
        with connection.cursor() as cursor:
            before = {}
            for kind, _external, payload in facts:
                cursor.execute("SELECT payload_json,payload_sha256 FROM ads_ai." + TABLE_BY_KIND[kind])
                rows = list(cursor.fetchall())
                if len(rows) != 1 or json.loads(rows[0]["payload_json"]) != payload or rows[0]["payload_sha256"] != _sha(_json_bytes(payload)):
                    raise RuntimeError("rehearsal full frozen payload roundtrip failed")
                before[kind] = rows
            rerun = _run_bootstrap(cursor, apply=True)
            if rerun["created_tables"] or not rerun["complete"]:
                raise RuntimeError("rehearsal compatible rerun was not a no-op")
            for kind, rows in before.items():
                cursor.execute("SELECT payload_json,payload_sha256 FROM ads_ai." + TABLE_BY_KIND[kind])
                if list(cursor.fetchall()) != rows:
                    raise RuntimeError("rehearsal changed existing records")
    finally:
        connection.close()
    return {"ok": True, "contract": REHEARSAL_CONTRACT, "schema": SCHEMA, "host": "127.0.0.1",
            "port": REHEARSAL_PORT, "engine_version": "5.7.44", "context": context,
            "candidate_git_sha": candidate_git_sha, "ddl_sha256": DDL_SHA256,
            "observed_at_utc": _now(), "runtime_identity_simulated": True,
            "checks": {key: True for key in REHEARSAL_CHECKS}, **container}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--rehearsal", action="store_true")
    parser.add_argument("--candidate-git-sha", default="")
    parser.add_argument("--evidence-file", default="")
    parser.add_argument("--rehearsal-context", default="")
    args = parser.parse_args()
    if args.rehearsal:
        if args.evidence_file:
            raise RuntimeError("old or production evidence is not accepted for a fresh rehearsal")
        result = rehearse(args.credential_file, candidate_git_sha=args.candidate_git_sha, context=args.rehearsal_context)
    else:
        if args.rehearsal_context:
            raise RuntimeError("rehearsal context cannot select a production target")
        result = bootstrap(args.credential_file, apply=args.apply, candidate_git_sha=args.candidate_git_sha, evidence_file=args.evidence_file)
    print(_json_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        result = {"ok": False, "error": "ads_ai_bootstrap_failed_existing_state_preserved"}
        if type(exc) is RuntimeError:
            result["reason"] = str(exc)
        elif isinstance(exc, LedgerRPCError):
            result["reason"] = exc.code
        elif exc.args and type(exc.args[0]) is int:
            result["database_error_code"] = exc.args[0]
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from None
