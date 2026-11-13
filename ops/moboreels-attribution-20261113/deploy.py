"""Phased, exact-SHA Moboreels deploy; never resets or replays publisher queues."""
import argparse
from contextlib import closing
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time

BASELINE = "49794d74"
PREVIOUS = "1742279d5ad07aaa0df5e5f86a49d9ccf6348e0b"
OPS = "ops/moboreels-attribution-20261113"
DATA = Path("/mnt/data-disk/moboreels-library")
CANDIDATES = Path("/mnt/data-disk/moboreels-social-releases")
CURRENT = Path("/opt/moboreels-library/current")
HEALTH = "/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py"
TABLES = {"cdreader_koc_order", "cdreader_koc_ad_revenue", "cdreader_koc_ad_day_snapshot",
          "cdreader_koc_short_link", "cdreader_koc_media_account"}
DDL_FILES = ("deploy/sql/moboreels-orders.sql", "deploy/sql/moboreels-short-links.sql",
             "deploy/moboreels-media-account.sql")
MACRO_USERS = ("x-post-automation", "x-post-daily", "fb-auto-post", "tt-post")
MAIN = Path("/root/drama_material_service")
PUBLIC = Path("/usr/share/nginx/html")


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def output(*args):
    return subprocess.check_output([str(x) for x in args], text=True).strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc():
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def save(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def atomic_copy(source, target, mode=0o644):
    target = Path(target)
    if not target.parent.exists():
        target.parent.mkdir(parents=True, mode=0o755)
        os.chmod(str(target.parent), 0o755)
    tmp = target.with_name(target.name + ".moboreels-next")
    require(not tmp.exists() and not tmp.is_symlink(), "Occupied temporary file")
    shutil.copy2(str(source), str(tmp))
    os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(target))


def switch(link, target, expected):
    link = Path(link)
    require(link.is_symlink() and str(link.resolve()) == str(expected), "Current link changed: " + str(link))
    tmp = link.with_name(link.name + ".moboreels-next")
    require(not tmp.exists() and not tmp.is_symlink(), "Occupied temporary link")
    tmp.symlink_to(target)
    os.replace(str(tmp), str(link))


def verify(release, sha):
    require(os.name == "posix" and os.geteuid() == 0, "CPU root only")
    require(os.path.samefile(sys.executable, "/usr/bin/python3"), "Use /usr/bin/python3")
    require(output("findmnt", "-n", "-o", "UUID", "/mnt/data-disk") ==
            "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8", "Data disk UUID differs")
    require(shutil.disk_usage("/mnt/data-disk").free > 2 * 1024**3, "Need 2 GiB free")
    require(re.fullmatch("[0-9a-f]{40}", sha), "Full commit SHA required")
    require(output("git", "-C", release, "rev-parse", "HEAD") == sha, "Checkout SHA differs")
    require(not output("git", "-C", release, "status", "--porcelain", "--untracked-files=no"), "Dirty checkout")
    require(output("git", "-C", release, "remote", "get-url", "origin") in
            ("git@github.com:gaoyuan12356416/ai-.git", "https://github.com/gaoyuan12356416/ai-.git"),
            "Unexpected Git remote")
    require(output("git", "-C", release, "for-each-ref", "--contains=" + sha,
                   "--format=%(refname)", "refs/remotes/origin"), "Fetch exact SHA from GitHub first")


def fence(entry, installed=False):
    target = Path(entry["target"])
    expected = entry["after"] if installed else entry["before"]
    require((target.is_file() and digest(target) == expected) if expected is not None
            else not target.exists() and not target.is_symlink(), "Live SHA changed: " + str(target))


def tree_digest(root):
    """Fence the whole cloned source tree so unrelated in-place edits cannot be lost."""
    rows = []
    for path in sorted(Path(root).rglob("*")):
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_symlink():
            value = "link:" + os.readlink(str(path))
        elif path.is_file():
            value = digest(path)
        else:
            value = "directory"
        rows.append((str(path.relative_to(root)), path.lstat().st_mode & 0o777, value))
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def stage(release, sha):
    baseline = json.loads((release / OPS / "baseline-manifest.json").read_text(encoding="utf-8"))
    by_remote = {x["remote_path"]: x for x in baseline["files"]}
    previous = CURRENT.resolve()
    require(previous.name == PREVIOUS, "Sidecar baseline changed")
    for row in baseline["roots"].values():
        require(str(Path(row["logical"]).resolve()) == row["resolved"], "Runtime current changed")
    plans = {}

    def add(source, target, phase, fallback=None, payload=None, nav=False):
        target = Path(target)
        require(str(target) not in plans, "Duplicate target: " + str(target))
        before = None
        if target.exists() or target.is_symlink():
            row = by_remote.get(str(target))
            before = row["sha256"] if row else digest(fallback) if fallback and Path(fallback).is_file() else None
            if nav:
                before = digest(target)
            require(before is not None, "Existing file has no baseline: " + str(target))
            require(target.is_file() and digest(target) == before, "Live file drift: " + str(target))
        content = payload if payload is not None else Path(source).read_bytes()
        after = hashlib.sha256(content).hexdigest()
        if after != before:
            plans[str(target)] = {"target": str(target), "source": str(source) if source else None,
                "phase": phase, "before": before, "after": after, "nav": nav,
                "mode": target.stat().st_mode & 0o777 if target.exists() else 0o644, "_content": content}

    prefix = OPS + "/runtime/"
    changed = output("git", "-C", release, "diff", "--name-only", BASELINE, sha).splitlines()
    for name in sorted(changed):
        source = release / name
        if not source.is_file():
            require(not name.startswith(("features/", "scripts/", "static/", prefix)),
                    "Source removal needs explicit review: " + name)
            continue
        if name.startswith(prefix):
            lane, relative = name[len(prefix):].split("/", 1)
            require(lane in ("x", "fb", "tt", "tt_auto"), "Unknown runtime")
            add(source, Path(baseline["roots"][lane]["logical"]) / relative, lane)
        elif name == "app.py":
            add(source, MAIN / name, "main")
        elif name.startswith("features/moboreels_library/"):
            add(source, MAIN / name, "core", previous / name)
        elif name.startswith(("features/", "scripts/")):
            add(source, MAIN / name, "main")
        elif name.startswith("static/"):
            phase = "core" if Path(name).name.startswith("moboreels-") else "main"
            for root in (MAIN / "static", PUBLIC):
                add(source, root / Path(name).name, phase, previous / name)
    # Three stdlib-only modules; never copy provider or DB credentials into worker trees.
    for lane in ("x", "fb", "tt", "tt_auto"):
        for name in ("__init__.py", "settings.py", "macro_client.py"):
            relative = "features/moboreels_library/" + name
            target = Path(baseline["roots"][lane]["logical"]) / relative
            if str(target) not in plans:
                add(release / relative, target, lane)
    for name in ("moboreels-report-sync.service", "moboreels-report-sync.timer"):
        add(release / "deploy" / name, Path("/etc/systemd/system") / name, "core")
    add(release / "deploy/nginx/moboreels-library.conf",
        Path("/etc/nginx/default.d/moboreels-library.conf"), "core",
        previous / "deploy/nginx/moboreels-library.conf")
    spec = importlib.util.spec_from_file_location("deploy_navigation", release / "features/moboreels_library/navigation.py")
    navigation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(navigation)
    for directory in (MAIN / "static", PUBLIC):
        target = directory / "navigation.json"
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        require(isinstance(config, list), "Navigation format changed")
        merged = navigation.ensure_navigation(navigation.ensure_report_navigation(config))
        if merged != config:
            add(None, target, "core", payload=(json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode(), nav=True)
    deployment = DATA / "deployments" / ("attribution-" + datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"))
    deployment.mkdir(mode=0o755)
    for name, mode in (("private", 0o700), ("staged", 0o700)):
        (deployment / name).mkdir(mode=mode)
    manifest = {"commit": sha, "release": str(release), "created_at": utc(), "roots": baseline["roots"],
                "previous_current": str(previous), "files": [], "candidates": {}, "phases": {},
                "source_trees": {}, "candidate_trees": {}}
    for i, entry in enumerate(plans.values()):
        staged = deployment / "staged" / ("%03d" % i)
        staged.write_bytes(entry.pop("_content"))
        os.chmod(str(staged), 0o600)
        if entry["target"].endswith(".py"):
            compile(staged.read_bytes(), entry["target"], "exec")
        entry.update(staged=str(staged), backup=str(deployment / "private" / ("file-%03d" % i)))
        manifest["files"].append(entry)
    for lane in ("x", "fb", "tt", "tt_auto"):
        original = Path(baseline["roots"][lane]["resolved"])
        require(not (original / "data").exists() and not list(original.glob("*.sqlite*")),
                "Runtime contains business data: " + lane)
        parent = CANDIDATES / sha / deployment.name
        for directory in (CANDIDATES, CANDIDATES / sha, parent):
            if not directory.exists():
                directory.mkdir(mode=0o755)
                os.chmod(str(directory), 0o755)
        manifest["source_trees"][lane] = tree_digest(original)
        candidate = parent / lane
        shutil.copytree(str(original), str(candidate), symlinks=True)
        os.chmod(str(candidate), 0o755)
        for entry in manifest["files"]:
            if entry["phase"] == lane:
                relative = Path(entry["target"]).relative_to(baseline["roots"][lane]["logical"])
                atomic_copy(entry["staged"], candidate / relative, entry["mode"])
        manifest["candidates"][lane] = str(candidate)
        require(tree_digest(original) == manifest["source_trees"][lane], "Source changed during clone")
        manifest["candidate_trees"][lane] = tree_digest(candidate)
        users = ("x-post-automation", "x-post-daily") if lane == "x" else (
            ("fb-auto-post",) if lane == "fb" else ("tt-post",))
        for user in users:
            run("runuser", "-u", user, "--", "test", "-r", candidate / "features/moboreels_library/macro_client.py")
    save(deployment / "manifest.json", manifest)
    shutil.copy2(str(release / OPS / "rollback.py"), str(deployment / "private" / "rollback.py"))
    print(json.dumps({"phase": "staged", "deployment": str(deployment), "files": len(plans), "activated": False}))


def database(release):
    sys.path.insert(0, str(release))
    from features.moboreels_library.settings import Settings
    from features.moboreels_library.db import Database
    return Database(Settings.from_env())


def split_sql(text):
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    statements, start, quote = [], 0, None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and (i == 0 or text[i-1] != "\\"):
                quote = None
        elif ch in ("'", '"', chr(96)):
            quote = ch
        elif ch == ";":
            statements.append(text[start:i].strip())
            start = i + 1
    require(not text[start:].strip() and quote is None, "Unterminated DDL")
    return [s for s in statements if s]


def expected_schema(statements):
    result = {}
    for statement in statements:
        table = re.match(r"CREATE TABLE IF NOT EXISTS ads_ai\.(\w+)", statement, re.I).group(1)
        columns, indexes = [], []
        for raw in statement.splitlines()[1:]:
            line = raw.strip().rstrip(",")
            column = re.match(r"(\w+)\s+((?:char|varchar|tinyint|smallint|bigint|int|text|json|datetime|date|decimal)(?:\([^)]*\))?(?:\s+UNSIGNED)?)(?=\s|$)(.*)", line, re.I)
            if column:
                name, kind, options = column.groups()
                comment = re.search(r"COMMENT\s+'([^']*)'", options, re.I)
                default = re.search(r"\bDEFAULT\s+(NULL|'[^']*'|[0-9]+)", options, re.I)
                collation = re.search(r"\bCOLLATE\s+(\w+)", options, re.I)
                columns.append({"column_name": name, "column_type": kind.lower(),
                    "nullable": "NO" if "NOT NULL" in options.upper() else "YES",
                    "default": None if not default or default.group(1).upper() == "NULL" else default.group(1).strip("'"),
                    "comment": comment.group(1) if comment else "",
                    "collation": collation.group(1) if collation else
                        "utf8mb4_unicode_ci" if re.match("(char|varchar|text)", kind, re.I) else None,
                    "extra": ""})
            else:
                match = re.match(r"(PRIMARY KEY|UNIQUE KEY\s+(\w+)|KEY\s+(\w+))\s*\((.*)\)", line, re.I)
                if match:
                    name = "PRIMARY" if match.group(1).upper() == "PRIMARY KEY" else match.group(2) or match.group(3)
                    non_unique = 0 if name == "PRIMARY" or match.group(2) else 1
                    for position, part in enumerate(match.group(4).split(","), 1):
                        col = re.fullmatch(r"\s*(\w+)(?:\((\d+)\))?\s*", part)
                        require(col is not None, "Unrecognized reviewed index")
                        indexes.append((name, non_unique, position, col.group(1), int(col.group(2)) if col.group(2) else None))
        table_comment = re.search(r"\)\s*ENGINE=InnoDB.*?COMMENT='([^']*)'\s*$", statement, re.I | re.S)
        require(table_comment is not None, "Unrecognized reviewed table options")
        result[table] = {"columns": columns, "indexes": sorted(indexes), "comment": table_comment.group(1)}
    return result


def validate_schema(statements, rows):
    expected = expected_schema(statements)
    normalize_type = lambda kind: re.sub(r"\b(tinyint|smallint|mediumint|bigint|int)\([0-9]+\)", r"\1", kind.lower())
    for table, definition in expected.items():
        actual = [r for r in rows if r.get("check") == "column" and r["table_name"] == table]
        actual.sort(key=lambda r: r["position"])
        require(len(actual) == len(definition["columns"]), "Column count differs: " + table)
        for source, observed in zip(definition["columns"], actual):
            for key in ("column_name", "nullable", "default", "comment", "collation", "extra"):
                require(source[key] == observed[key], "Column definition differs: " + table + "." + source["column_name"] + ":" + key)
            require(normalize_type(source["column_type"]) == normalize_type(observed["column_type"]),
                    "Column type differs: " + table + "." + source["column_name"])
        actual_indexes = sorted((r["index_name"], int(r["non_unique"]), int(r["position"]),
                                 r["column_name"], r["prefix_length"])
                                for r in rows if r.get("check") == "index" and r["table_name"] == table)
        require(actual_indexes == definition["indexes"], "Index definition differs: " + table)
        observed_table = [r for r in rows if r.get("check") == "new_table" and r["table_name"] == table]
        require(len(observed_table) == 1 and observed_table[0]["engine"] == "InnoDB"
                and observed_table[0]["collation"] == "utf8mb4_unicode_ci"
                and observed_table[0]["comment"] == definition["comment"], "Table options differ: " + table)


def schema(release, deployment, manifest):
    require("schema" not in manifest["phases"], "Schema already recorded; inspect readback")
    db = database(release)
    query = (release / "deploy/sql/moboreels-preflight-readonly.sql").read_text(encoding="utf-8")
    before = db._read(query)
    save(deployment / "private" / "schema-before.json", before)
    require(not any(r["exists"] for r in before if r.get("check") == "new_table"),
            "Existing or partially created tables require exact schema review; do not blindly rerun")
    statements = []
    for name in DDL_FILES:
        statements += split_sql((release / name).read_text(encoding="utf-8"))
    matches = [re.match(r"^CREATE TABLE IF NOT EXISTS ads_ai\.(\w+)\s*\(", s, re.I) for s in statements]
    require(len(matches) == len(TABLES) and all(matches) and {m.group(1) for m in matches} == TABLES,
            "DDL must contain exactly the approved CREATE TABLE statements")
    save(deployment / "private" / "schema-intent.json", {"at": utc(), "tables": sorted(TABLES),
        "sha256": [digest(release / n) for n in DDL_FILES]})
    try:
        connection = db._connection()  # Fixed 63353; only identity/read_only validation, then reviewed DDL.
        for statement in statements:
            with connection.cursor() as cursor:
                cursor.execute(statement)
            connection.commit()
    finally:
        db.close_writer()
    for attempt in range(12):
        after = db._read(query)  # All metadata/readback uses 63350 and the FIFO gate.
        if sum(bool(r.get("exists")) for r in after if r.get("check") == "new_table") == len(TABLES):
            break
        time.sleep(2)
    save(deployment / "private" / "schema-after.json", after)
    require(sum(bool(r.get("exists")) for r in after if r.get("check") == "new_table") == len(TABLES),
            "Replica readback incomplete; reconcile before retry")
    validate_schema(statements, after)
    for table in TABLES:
        columns = [r for r in after if r.get("check") == "column" and r["table_name"] == table]
        require(columns and all(re.search("[\u4e00-\u9fff]", r.get("comment") or "") for r in columns),
                "Missing Chinese column remarks: " + table)
        require(any(r.get("check") == "new_table" and r["table_name"] == table
                    and re.search("[\u4e00-\u9fff]", r.get("comment") or "") for r in after),
                "Missing Chinese table remark: " + table)
    manifest["phases"]["schema"] = {"status": "verified", "completed_at": utc()}
    save(deployment / "manifest.json", manifest)
    print(json.dumps({"phase": "schema", "tables": len(TABLES), "chinese_comments": "verified"}))


def busy(spec, deployment, phase):
    rows = []
    require(spec.get("checks") or spec.get("explicit_no_ledger_reason"), "Missing reviewed busy checks")
    for item in spec.get("checks", []) + spec.get("informational_checks", []):
        path = Path(item["database"]).resolve()
        query = item["sql"]
        require(path.is_file() and re.match(r"^\s*SELECT\s+COUNT\s*\(", query, re.I) and ";" not in query,
                "Busy check must be one read-only COUNT over an existing ledger")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as conn:
            conn.execute("PRAGMA query_only=ON")
            count = int(conn.execute(query).fetchone()[0])
        rows.append({"name": item["name"], "busy": count, "database": str(path),
                     "informational": item in spec.get("informational_checks", [])})
    for unit in spec.get("must_be_inactive", []):
        require(output("systemctl", "show", unit, "--property=ActiveState", "--value") == "inactive",
                "Scoped admission/runner still active: " + unit)
    save(deployment / "private" / ("busy-" + phase + ".json"), {"at": utc(), "checks": rows})
    require(not any(r["busy"] for r in rows if not r["informational"]), "In-flight work; defer this lane")


def backups(spec, deployment, phase):
    directory = deployment / "private" / ("ledgers-" + phase + "-" + datetime.datetime.utcnow().strftime("%H%M%S"))
    directory.mkdir(mode=0o700)
    paths = set(spec.get("backup_databases", []) + [r["database"] for r in spec.get("checks", [])])
    for i, name in enumerate(sorted(paths)):
        path = Path(name).resolve()
        require(path.is_file(), "Missing ledger: " + name)
        destination = directory / ("%02d-%s" % (i, path.name))
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as src:
            with closing(sqlite3.connect(str(destination))) as dst:
                src.backup(dst, pages=256, sleep=0.05)
                require(dst.execute("PRAGMA quick_check").fetchone()[0] == "ok", "Backup integrity failed")
        os.chmod(str(destination), 0o600)


def scheduler(spec, deployment, phase, restore=False):
    for lane in spec.get("scheduler_lanes", []):
        require(lane in ("x", "fb", "tt", "youtube"), "Unexpected scheduler lane")
        before = deployment / "private" / ("scheduler-" + phase + "-" + lane + "-before.json")
        if restore:
            run(sys.executable, HEALTH, "restore", "--baseline", before, "--wait-seconds", "25",
                "--output", deployment / "private" / ("scheduler-" + phase + "-" + lane + "-after.json"))
        else:
            if before.exists():
                continue  # Preserve the original pre-quiesce baseline on resume.
            provided = spec.get("scheduler_baselines", {}).get(lane)
            if provided:
                shutil.copy2(str(provided), str(before))
                os.chmod(str(before), 0o600)
            else:
                require(not spec.get("must_be_inactive"),
                        "Provide scheduler baseline captured before scoped admission/timer pause")
                run(sys.executable, HEALTH, "snapshot", "--lane", lane, "--output", before)


def macro_config():
    from features.moboreels_library.settings import read_env_file
    provider = Path("/etc/moboreels-library.env")
    require(provider.stat().st_mode & 0o077 == 0, "Provider config must remain root-only")
    institution = read_env_file(provider).get("MOBOREELS_INSTITUTION_USER_ID", "")
    require(re.fullmatch("[a-zA-Z0-9_-]{1,64}", institution), "Invalid institution ID")
    path = Path("/etc/moboreels-macro.env")
    if path.exists():
        config = read_env_file(path)
        require(set(config) == {"MOBOREELS_MACRO_TOKEN", "MOBOREELS_INSTITUTION_USER_ID"} and
                config["MOBOREELS_INSTITUTION_USER_ID"] == institution and
                len(config["MOBOREELS_MACRO_TOKEN"]) >= 32, "Existing macro config requires review")
    else:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("MOBOREELS_MACRO_TOKEN=" + secrets.token_urlsafe(48) +
                         "\nMOBOREELS_INSTITUTION_USER_ID=" + institution + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    run("setfacl", "-b", path)
    os.chmod(str(path), 0o600)
    for user in MACRO_USERS:
        run("id", "-u", user, stdout=subprocess.DEVNULL)
        run("setfacl", "-m", "u:" + user + ":r--", path)
        run("runuser", "-u", user, "--", "test", "-r", path)


def activate(release, deployment, manifest, phase, checks, resume=False):
    started = manifest["phases"].get(phase)
    require(not started or resume, "Phase already started; inspect manifest then use --resume")
    if started and started.get("status") == "activated":
        print(json.dumps({"phase": phase, "status": "already_activated"}))
        return
    require(manifest["phases"].get("schema", {}).get("status") == "verified", "Verify schema first")
    require(phase == "core" or manifest["phases"].get("core", {}).get("status") == "activated", "Activate core first")
    if phase == "main":
        require(all(manifest["phases"].get(p, {}).get("status") == "activated" for p in ("x", "fb", "tt", "tt_auto")),
                "Activate social owners before the main API compatibility switch")
    require(checks.get("commit") == manifest["commit"], "Checks must name exact final SHA")
    spec = checks.get("phases", {}).get(phase)
    require(isinstance(spec, dict), "Missing reviewed activation checks")
    entries = [r for r in manifest["files"] if r["phase"] == phase]
    for entry in entries:
        require(digest(entry["staged"]) == entry["after"], "Staged source changed")
        if started and Path(entry["target"]).is_file() and digest(entry["target"]) == entry["after"]:
            continue
        fence(entry)
    if phase in manifest["candidates"]:
        root = manifest["roots"][phase]
        live = str(Path(root["logical"]).resolve())
        require(live == root["resolved"] or started and live == manifest["candidates"][phase], "Lane current drift")
        require(tree_digest(root["resolved"]) == manifest["source_trees"][phase], "Cloned live source changed")
        require(tree_digest(manifest["candidates"][phase]) == manifest["candidate_trees"][phase], "Candidate changed")
    busy(spec, deployment, phase)
    try:
        scheduler(spec, deployment, phase)
        backups(spec, deployment, phase)
        busy(spec, deployment, phase)
    except Exception:
        scheduler(spec, deployment, phase, restore=True)
        raise
    try:
        for entry in entries:
            if started:
                require(entry["before"] is None or Path(entry["backup"]).is_file(), "Original backup missing")
                continue
            fence(entry)
            if entry["before"] is not None:
                shutil.copy2(entry["target"], entry["backup"])
                os.chmod(entry["backup"], 0o600)
        manifest["phases"][phase] = {"status": "installing", "started_at": started.get("started_at") if started else utc(), "spec": spec}
        save(deployment / "manifest.json", manifest)
    except Exception:
        scheduler(spec, deployment, phase, restore=True)
        raise
    try:
        if phase in manifest["candidates"]:
            root = manifest["roots"][phase]
            if str(Path(root["logical"]).resolve()) != manifest["candidates"][phase]:
                switch(root["logical"], manifest["candidates"][phase], root["resolved"])
        else:
            for entry in entries:
                atomic_copy(entry["staged"], entry["target"], entry["mode"])
        if phase == "core":
            database(release)
            macro_path = Path("/etc/moboreels-macro.env")
            if macro_path.exists() and not started:
                shutil.copy2(str(macro_path), str(deployment / "private" / "macro.env.before"))
            macro_config()
            run("nginx", "-t")
            if str(CURRENT.resolve()) != str(release):
                switch(CURRENT, release, manifest["previous_current"])
            run("systemctl", "daemon-reload")
            run("systemctl", "restart", "moboreels-library.service")
            run("systemctl", "reload", "nginx")
            run("systemctl", "enable", "--now", "moboreels-report-sync.timer")
        for unit in spec.get("restart_services", []):
            require(re.fullmatch("[a-z0-9@_.-]+[.]service", unit), "Invalid unit")
            run("systemctl", "restart", unit)
            run("systemctl", "is-active", unit)
        if not spec.get("defer_scheduler_restore"):
            scheduler(spec, deployment, phase, restore=True)
        for entry in entries:
            fence(entry, installed=True)
        if phase == "core":
            run("systemctl", "is-active", "moboreels-library.service", "moboreels-report-sync.timer")
        manifest["phases"][phase].update(status="activated", completed_at=utc())
    except Exception:
        manifest["phases"][phase].update(status="needs_reconciliation", failed_at=utc())
        save(deployment / "manifest.json", manifest)
        raise RuntimeError("Activation stopped; inspect durable manifest; retain /m/ and all ledgers") from None
    save(deployment / "manifest.json", manifest)
    print(json.dumps({"phase": phase, "status": "activated", "commit": manifest["commit"]}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sha")
    parser.add_argument("phase", choices=("stage", "schema", "core", "main", "x", "fb", "tt", "tt_auto"))
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--checks", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[2]
    verify(release, args.sha)
    if args.phase == "stage":
        require(args.deployment is None, "Stage creates a fresh deployment")
        stage(release, args.sha)
        return
    require(args.deployment is not None, "--deployment required")
    deployment = args.deployment.resolve()
    require(deployment.parent == (DATA / "deployments").resolve(), "Invalid deployment directory")
    manifest = json.loads((deployment / "manifest.json").read_text(encoding="utf-8"))
    require(manifest["commit"] == args.sha and manifest["release"] == str(release), "Manifest release differs")
    if args.phase == "schema":
        schema(release, deployment, manifest)
    else:
        require(args.checks is not None, "--checks required")
        activate(release, deployment, manifest, args.phase,
                 json.loads(args.checks.read_text(encoding="utf-8")), resume=args.resume)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "stopped", "type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, RuntimeError) else "inspect restricted evidence"}))
        sys.exit(1)

