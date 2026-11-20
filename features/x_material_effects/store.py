"""Immutable local serving snapshot; MySQL remains the reusable warehouse."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from .model import METRICS, aggregate

ROOT = '/mnt/data-disk/x-material-effects'

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def atomic_json(path, value):
    path = Path(path); temporary = path.with_suffix('.next')
    with temporary.open('w', encoding='utf-8') as f:
        f.write(encoded(value)); f.flush(); os.fsync(f.fileno())
    os.replace(temporary, path)

def current(root=None):
    root = Path(root or os.getenv('X_MATERIAL_EFFECT_ROOT', ROOT))
    meta = json.loads((root/'current.json').read_text())
    path = (root/meta['database']).resolve()
    if path.parent != (root/'snapshots').resolve(): raise ValueError('invalid_effect_snapshot')
    return meta, path

def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro&immutable=1', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    return db

def publish(root, version, posts, meta):
    root = Path(root); directory = root/'snapshots'; directory.mkdir(parents=True, exist_ok=True)
    target = directory/(version+'.sqlite3'); temporary = target.with_suffix('.next')
    materials = aggregate(posts)
    with closing(sqlite3.connect(temporary)) as db:
        db.execute('CREATE TABLE posts(post_id TEXT PRIMARY KEY,namespace TEXT,material_id TEXT,account_id TEXT,product TEXT,language TEXT,content_id TEXT,published_at TEXT,payload TEXT)')
        db.executemany('INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,?)',
            [(p['post_id'],p['namespace'],p['material_id'],str(p['account_id']),p['product'],p['language'],p['content_id'],p['published_at'],encoded(p)) for p in posts])
        db.execute('CREATE INDEX posts_material ON posts(namespace,material_id,published_at)')
        db.execute('CREATE INDEX posts_account ON posts(account_id,published_at)')
        db.execute('CREATE INDEX posts_product ON posts(product,language,content_id,published_at)')
        db.execute('CREATE TABLE materials(namespace TEXT,material_id TEXT,product TEXT,catalog TEXT,content_id TEXT,language TEXT,payload TEXT,PRIMARY KEY(namespace,material_id))')
        db.executemany('INSERT INTO materials VALUES(?,?,?,?,?,?,?)',[(r['namespace'],r['material_id'],r['product'],r['catalog'],r['content_id'],r['language'],encoded(r)) for r in materials])
        db.execute('CREATE INDEX material_drama ON materials(catalog,content_id,language,product)')
        db.commit()
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise ValueError('effect_snapshot_corrupt')
    os.replace(temporary,target)
    old = json.loads((root/'current.json').read_text()) if (root/'current.json').exists() else {}
    manifest = dict(meta, version=version, database='snapshots/'+target.name,
                    post_count=len(posts), material_count=len(materials), previous=old.get('database'))
    atomic_json(root/'current.json',manifest)
    # Keep the previous generation for requests that already captured its pointer.
    keep = {manifest['database'],old.get('database')}
    for p in directory.glob('*.sqlite3'):
        if str(p.relative_to(root)) not in keep and (datetime.now(timezone.utc).timestamp()-p.stat().st_mtime)>86400:
            p.unlink()
    return manifest

def rows(path, table='materials'):
    if table not in ('materials','posts'): raise ValueError('invalid_table')
    with closing(connect(path)) as db:
        return [json.loads(r[0]) for r in db.execute('SELECT payload FROM '+table)]
