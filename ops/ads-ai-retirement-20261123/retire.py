"""Freeze each retired group, check the frozen rows against its archive, then drop."""
import argparse
import concurrent.futures
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import archive

PREFIX = "ret261123_"
RENAME_PAIR = re.compile(r"`ads_ai`\.`(\w+)` TO `ads_ai`\.`(\w+)`")
FOLDER = Path(__file__).resolve().parent


def migration():
    allowed = set(archive.TABLES)
    groups = []
    for sql in (FOLDER / "rename.sql").read_text().strip().rstrip(";").split(";"):
        pairs = RENAME_PAIR.findall(sql)
        expected = "RENAME TABLE\n" + ",\n".join(
            "  `ads_ai`.`%s` TO `ads_ai`.`%s`" % pair for pair in pairs)
        if not pairs or sql.strip() != expected or any(b != PREFIX + a or a not in allowed for a, b in pairs):
            raise ValueError("invalid rename statement")
        groups.append((sql.strip(), dict(pairs)))
    names = [k for _, group in groups for k in group]
    if len(names) != len(allowed) or set(names) != allowed:
        raise ValueError("rename scope differs from archive")
    ordered = {}
    for sql in (FOLDER / "drop.sql").read_text().splitlines():
        match = re.fullmatch(r"DROP TABLE `ads_ai`\.`ret261123_(\w+)`;", sql)
        if not match or match[1] in ordered:
            raise ValueError("invalid drop statement")
        ordered[match[1]] = sql
    if set(ordered) != allowed:
        raise ValueError("drop scope differs from archive")
    return groups, ordered


def inventory(local):
    with closing(archive.connect(local=local)) as db, db.cursor() as cur:
        cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='ads_ai'")
        return {v[0] for v in cur.fetchall()}


def settle(local, present, absent):
    deadline = time.monotonic() + 45
    while True:
        names = inventory(local)
        if present <= names and not absent & names:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("DDL readback incomplete")
        time.sleep(1.5)


def exec_ddl(local, statements):
    with closing(archive.connect(local=local, writer=True)) as db, db.cursor() as cur:
        cur.execute("SET SESSION FOREIGN_KEY_CHECKS=1")
        for sql in statements:
            cur.execute(sql)


def schema_check(local, group):
    known = (archive.BUNDLE / "schema.sql").read_text()
    names = tuple(group)
    placeholders = ",".join(["%s"] * len(names))
    with closing(archive.connect(local=local)) as db, db.cursor() as cur:
        if not local:
            for name in names:
                cur.execute("SHOW CREATE TABLE `" + name + "`")
                if cur.fetchone()[1] + ";\n" not in known:
                    raise RuntimeError("definition changed: " + name)
        cur.execute("SELECT TABLE_SCHEMA,TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE REFERENCED_TABLE_SCHEMA='ads_ai' AND REFERENCED_TABLE_NAME IN (" +
                    placeholders + ")", names)
        if any(s != "ads_ai" or t not in group for s, t in cur.fetchall()):
            raise RuntimeError("foreign key from outside retired group")
        cur.execute("SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE EVENT_OBJECT_SCHEMA='ads_ai' "
                    "AND EVENT_OBJECT_TABLE IN (" + placeholders + ")", names)
        if cur.fetchone():
            raise RuntimeError("trigger not included in archived schema")


class TupleStream:
    """Hash the ordered SQL tuples independently of INSERT statement boundaries."""
    def __init__(self, table):
        self.prefix = ("INSERT INTO `" + table + "` VALUES ").encode()
        self.digest = hashlib.sha256()
        self.has_rows = False

    def add(self, line):
        if not line.startswith(b"INSERT "):
            return
        statement = line.rstrip(b"\r\n")
        if not statement.startswith(self.prefix) or not statement.endswith(b";"):
            raise ValueError("unexpected data statement in dump")
        if self.has_rows:
            self.digest.update(b",")
        self.has_rows = True
        self.digest.update(memoryview(statement)[len(self.prefix):-1])


def original_tuples(part, local_json=False):
    rows = TupleStream(part["table"])
    if local_json:
        cmd, env = archive.dump_command(part, True)
        with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            for line in proc.stdout:
                rows.add(line)
            if proc.wait():
                raise RuntimeError("local dump failed")
    else:
        with gzip.open(archive.BUNDLE / part["file"], "rb") as stream:
            for line in stream:
                rows.add(line)
    return rows.digest.hexdigest()


def frozen_digest(part, local, local_json_hashes):
    cmd, env = archive.dump_command(part, local)
    name = part["table"]
    cmd[-1] = PREFIX + name
    before = ("INSERT INTO `" + cmd[-1] + "` ").encode()
    after = ("INSERT INTO `" + name + "` ").encode()
    digest = hashlib.sha256()
    tuples = TupleStream(cmd[-1])
    with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        for line in proc.stdout:
            tuples.add(line)
            if line.startswith(before):
                line = after + line[len(before):]
            digest.update(line)
        if proc.wait():
            raise RuntimeError("frozen read failed: " + part["file"])
    expected = part["sql_sha256"]
    if local:
        recorded = json.loads((archive.ROOT / "restore-verified.json").read_text())
        expected = recorded["restored_sql_sha256"][part["file"]]
    if digest.hexdigest() != expected:
        # The longer frozen name can change where mysqldump starts a new INSERT.
        # Compare the complete value stream, including every separator and value.
        expected_tuples = local_json_hashes.get(name) or original_tuples(part)
        if tuples.digest.hexdigest() != expected_tuples:
            raise RuntimeError("frozen content differs from the backup: " + part["file"])
    print(archive.stamp(), "frozen data matched", part["file"], flush=True)


def process(local, group_id, sql, group, drops, manifest, receipt_dir):
    names = inventory(local)
    original = set(group)
    frozen = set(group.values())
    proof = receipt_dir / ("group-%d-frozen-proof.json" % group_id)
    json_hash_file = receipt_dir / ("group-%d-local-json.json" % group_id)
    local_json_hashes = json.loads(json_hash_file.read_text()) if json_hash_file.exists() else {}
    manifest_hash = archive.file_hash(archive.BUNDLE / "manifest.json")
    partial = proof.is_file() and json.loads(proof.read_text())["manifest_sha256"] == manifest_hash
    if not original & names and not frozen & names and partial:
        return
    if original <= names and not frozen & names:
        schema_check(local, group)
        if local:
            for part in manifest["parts"]:
                name = part["table"]
                if name in group and name in archive.JSON_TARGETS:
                    local_json_hashes[name] = original_tuples(part, local_json=True)
            archive.write_json(json_hash_file, local_json_hashes)
        exec_ddl(local, [sql])
    elif original & names or (not frozen <= names and not partial):
        raise RuntimeError("unexpected mix of old, frozen, or missing names")
    if not partial:
        settle(local, frozen, original)
        try:
            parts = [part for part in manifest["parts"] if part["table"] in group]
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda part: frozen_digest(part, local, local_json_hashes), parts))
        except Exception:
            # All originals are still held under the private frozen names here.
            observed = inventory(local)
            if frozen <= observed and not original & observed:
                reverse = ", ".join("`ads_ai`.`%s` TO `ads_ai`.`%s`" % (b, a) for a, b in group.items())
                exec_ddl(local, ["RENAME TABLE " + reverse])
                settle(local, original, frozen)
            raise
        archive.write_json(proof, dict(at=archive.stamp(), manifest_sha256=manifest_hash,
                                       parts=len(parts), tables=sorted(original)))
    # Never drop anything under a pre-retirement name, even if recreated.
    remaining = inventory(local) & frozen
    exec_ddl(local, [sql for name, sql in drops.items() if group.get(name) in remaining])
    settle(local, set(), frozen | original)
    archive.write_json(receipt_dir / ("group-%d-removed.json" % group_id),
                       dict(at=archive.stamp(), tables=sorted(original)))
    print(archive.stamp(), "cleared", group_id, len(group), flush=True)


def run(local):
    manifest = archive.read_bundle()
    archive_hash = archive.file_hash(archive.BUNDLE / "manifest.json")
    verified = json.loads((archive.ROOT / "restore-verified.json").read_text())
    if verified["manifest_sha256"] != archive_hash:
        raise RuntimeError("rehearsed archive differs from the current archive")
    if not local:
        ddl_proof = json.loads((archive.ROOT / "test-ddl/result.json").read_text())
        if not ddl_proof["complete"] or ddl_proof["manifest_sha256"] != archive_hash:
            raise RuntimeError("DDL rehearsal incomplete")
    groups, drops = migration()
    directory = archive.ROOT / ("test-ddl" if local else "retirement")
    directory.mkdir(mode=0o700, exist_ok=True)
    baseline = directory / "before.json"
    if not baseline.exists():
        archive.write_json(baseline, sorted(inventory(local)))
    failures = {}
    for i, (sql, group) in enumerate(groups, 1):
        try:
            process(local, i, sql, group, drops, manifest, directory)
        except Exception as exc:
            failures[str(i)] = str(exc)[:300]
            print(archive.stamp(), "group failed", i, failures[str(i)], flush=True)
    after = inventory(local)
    before = set(json.loads(baseline.read_text()))
    extras_removed = sorted((before-set(archive.TABLES))-after)
    remaining = sorted(after & (set(archive.TABLES) | {PREFIX+n for n in archive.TABLES}))
    success = not failures and not remaining and not extras_removed
    archive.write_json(directory / "result.json", dict(at=archive.stamp(), complete=success,
                       manifest_sha256=archive_hash, failures=failures, remaining=remaining,
                       unrelated_names_missing=extras_removed, removed=sorted(before-after),
                       surviving_count=len(after)))
    if not success:
        raise RuntimeError("retirement has outstanding groups or inventory changes")


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["rehearse-ddl", "apply"])
    run(parser.parse_args().action == "rehearse-ddl")
