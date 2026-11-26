"""Publish and consume compact, content-addressed matching inputs."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unicodedata

SUPPORTED_PRODUCTS = (1479, 979, 3302)
DEFAULT_ROOT = "/data/fb-page-assignment-v2/catalog"
VERSION = re.compile(r"\d{8}T\d{12}Z")
HEX_HASH = re.compile(r"[0-9a-f]{64}")


def canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def language(value):
    key = str(value or "").strip().lower().replace("_", "-")
    return {"jp": "ja", "pt": "pt-br"}.get(key, key)


def topics(rows, aliases, themes):
    by_lang = defaultdict(list)
    for row in rows:
        by_lang[language(row["language"])].append(row)
    out = {}
    for lang, entries in by_lang.items():
        series = {str(row.get("series_code") or "") for row in entries} - {""}
        labels = set()
        if len(series) <= 1:
            for row in entries:
                raw = row.get("labels") or []
                if isinstance(raw, str):
                    try:
                        items = json.loads(raw)
                    except (ValueError, TypeError):
                        items = None
                    raw = items if isinstance(items, list) else raw.split(",")
                for item in raw if isinstance(raw, list) else ():
                    if isinstance(item, str):
                        key = unicodedata.normalize("NFKC", item).strip().casefold()
                        theme = themes.get(aliases.get(key))
                        if theme and theme != "__unknown__":
                            labels.add(theme)
        out[lang] = sorted(labels)
    return out


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        tmp = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
        if os.name == "posix":
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def manifest(path):
    head = json.loads(path.read_bytes())
    if (head.get("format") != 1 or not VERSION.fullmatch(str(head.get("version", "")))
            or not HEX_HASH.fullmatch(str(head.get("sha256", "")))):
        raise ValueError("invalid_catalog_manifest")
    return head


def read_bundle(source, head):
    packed = (source / (head["sha256"] + ".json.gz")).read_bytes()
    if head.get("packed_sha256") and sha(packed) != head["packed_sha256"]:
        raise ValueError("catalog_archive_hash_mismatch")
    raw = gzip.decompress(packed)
    if sha(raw) != head["sha256"]:
        raise ValueError("catalog_content_hash_mismatch")
    obj = json.loads(raw)
    if (obj["format"] != 1 or obj["version"] != head["version"]
            or not obj["pages"] or len(obj["pages"]) != head["page_count"]
            or not obj["directions"] or not obj["themes"]
            or not isinstance(obj["aliases"], dict)):
        raise ValueError("invalid_catalog_bundle")
    return obj, packed


def install(staging, home):
    head = manifest(staging / "current.json")
    _, packed = read_bundle(staging, head)
    home.mkdir(parents=True, exist_ok=True)
    from contextlib import nullcontext
    lock = (home / "publish.lock").open("a") if os.name == "posix" else nullcontext()
    with lock as fd:
        if os.name == "posix":
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        current = home / "current.json"
        if current.exists() and manifest(current)["version"] > head["version"]:
            return manifest(current)
        archive = home / (head["sha256"] + ".json.gz")
        if not archive.exists() or sha(archive.read_bytes()) != sha(packed):
            atomic(archive, packed)
        atomic(current, canon(head))
    return head


class Catalog:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("FB_PAGE_V2_CATALOG", DEFAULT_ROOT))
        self.head = self.data = self._stamp = None
        self.lock = threading.Lock()

    def current(self):
        pointer = self.home / "current.json"
        with self.lock:
            stat = pointer.stat()
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if self._stamp != stamp:
                head = manifest(pointer)
                if self.head is None or self.head["sha256"] != head["sha256"]:
                    self.data = read_bundle(self.home, head)[0]
                self.head, self._stamp = head, stamp
            return self.head, self.data


def export(out, current, projection, audience, shared_root):
    """Use the *published* page version and its completed collection only."""
    root = current.parent
    pointer = json.loads(current.read_bytes())
    version = pointer["version"]
    if pointer.get("database") != "ads_ai.fb_page_revenue_profile" or not VERSION.fullmatch(version):
        raise ValueError("unverified_page_publication")
    if out is not None and (out / "current.json").exists():
        saved = manifest(out / "current.json")
        if saved["version"] == version and (out / (saved["sha256"] + ".json.gz")).is_file():
            return saved
    with closing(sqlite3.connect(projection.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        payload, scope, n = db.execute("SELECT payload,scope,n FROM head").fetchone()
        meta = json.loads(payload)
        if version != meta["version"] or n != meta["page_count"]:
            raise ValueError("page_publication_changed")
        pages = {pid: json.loads(raw) for pid, raw in db.execute("SELECT page_id,projection FROM pages")}
        if (len(pages) != n or sha(canon(sorted(pages))) != scope
                or any(p.get("page_id") != pid for pid, p in pages.items())):
            raise ValueError("incomplete_published_projection")
    receipt = json.loads((root / (version + ".receipt.json")).read_bytes())
    if (receipt.get("version") != version or receipt.get("verified_rows") != n
            or set(receipt["payload_hashes"]) != set(pages)):
        raise ValueError("unverified_page_scope")
    import features
    shared = str(shared_root / "features")
    if shared not in features.__path__:
        features.__path__.append(shared)
    from features.x_tag_revenue.tags import ALIASES, VERSION as tags_version
    from features.x_tag_revenue.taxonomy import TAG_THEME
    directions = {}
    with closing(sqlite3.connect(audience.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        for app, cid, langs, key in db.execute(
                "SELECT app_id,content_id,languages_json,audience FROM drama_audience "
                "WHERE app_id IN (1479,979,3302) AND status='matched'"):
            directions.setdefault(str(app), {})[str(cid)] = [key, list(map(language, json.loads(langs)))]
    if not directions.get("1479"):
        raise ValueError("authoritative_directions_missing")
    manifests = (root / "collect-cache").glob("*/manifest.json")
    folder = None
    for marker in manifests:
        item = json.loads(marker.read_bytes())
        if item.get("started_at") == meta["collection_started_at"] and item.get("complete"):
            folder = marker.parent
            break
    if folder is None:
        raise ValueError("matching_collection_not_ready")
    resources = defaultdict(list)
    ids = set()
    for f in folder.glob("*.json"):
        if f.name in ("manifest.json", "social-input.json"):
            continue
        rows = json.loads(f.read_bytes()).get("rows", [])
        if not rows or "series_code" not in rows[0]:
            continue
        if "labels" not in rows[0] and "resource_type" not in rows[0]:
            continue
        for row in rows:
            key = (str(row["app_id"]), str(row["content_id"]))
            ids.add(key)
            if "labels" in row:
                resources[key].append(row)
    if not ids or not resources:
        raise ValueError("matching_collection_empty")
    warm = {}
    for app, cid in sorted(ids):
        warm.setdefault(app, {})[cid] = topics(resources[(app, cid)], ALIASES, TAG_THEME)
    data = dict(format=1, version=version, pages=pages, directions=directions, warm=warm,
                aliases=ALIASES, themes=TAG_THEME, tag_version=tags_version, page_scope=scope)
    raw = canon(data)
    digest = sha(raw)
    head = dict(format=1, version=version, sha256=digest, page_count=n,
                warm_titles=len(ids), generated_at=datetime.now(timezone.utc).isoformat())
    if json.loads(current.read_bytes())["version"] != version:
        raise ValueError("page_publication_changed")
    if out is not None:
        packed = gzip.compress(raw, compresslevel=4, mtime=0)
        head["packed_sha256"] = sha(packed)
        atomic(out / (digest + ".json.gz"), packed)
        atomic(out / "current.json", canon(head))
    return head


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    exp = commands.add_parser("export")
    exp.add_argument("--check-only", action="store_true", help="validate live inputs without writing")
    exp.add_argument("--out", type=Path, default=Path("/mnt/data-disk/fb-page-v2/catalog"))
    exp.add_argument("--current", type=Path, default=Path("/mnt/data-disk/fb-page-portraits/current.json"))
    exp.add_argument("--projection", type=Path, default=Path("/mnt/data-disk/fb-page-current-20261123/selection/current.sqlite3"))
    exp.add_argument("--audience", type=Path, default=Path("/mnt/data-disk/drama-audience/catalog.sqlite3"))
    exp.add_argument("--shared-root", type=Path, default=Path("/root/drama_material_service"))
    imp = commands.add_parser("install")
    imp.add_argument("--staging", type=Path, required=True)
    imp.add_argument("--home", type=Path, default=Path(DEFAULT_ROOT))
    args = parser.parse_args()
    result = (export(None if args.check_only else args.out, args.current, args.projection, args.audience, args.shared_root)
              if args.command == "export" else install(args.staging, args.home))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
