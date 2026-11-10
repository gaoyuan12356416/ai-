"""Indexed disk projections of verified ads_ai portraits; no resident catalog cache."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import zlib

from .storage import Store as SQLStore, PAGE_SIZE, VERSION_RE, encoded, projections
from features.social_analytics.warehouse_refresh import atomic_json

DEFAULT_ROOT = '/mnt/data-disk/x-channel-tag-revenue'


def fingerprint(row):
    return zlib.crc32(encoded(row).encode('utf-8'))


def publish(root, data, proof):
    version = data['meta']['version']
    if not re.fullmatch(VERSION_RE, version) or proof['version'] != version:
        raise ValueError('invalid_verified_profile_version')
    if proof['rows'] != len(data['accounts']) + 1 or proof['revenue'] != sum(p['revenue_cents'] for p in data['accounts'].values()):
        raise ValueError('profile_source_proof_mismatch')
    directory = Path(root) / 'serving'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (version + '.sqlite3')
    temporary = directory / (version + '.pending.sqlite3')
    temporary.unlink(missing_ok=True)
    counts, checksums = {'profiles': 0, 'dramas': 0}, {'profiles': 0, 'dramas': 0}
    meta = dict(data['meta'], storage='ads_ai.x_channel_revenue_profile', audit=data.get('audit', {}))
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript('''
                CREATE TABLE metadata (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE profiles (x_user_id TEXT PRIMARY KEY, summary TEXT NOT NULL, tree TEXT NOT NULL) WITHOUT ROWID;
                CREATE TABLE dramas (x_user_id TEXT NOT NULL, path TEXT NOT NULL, position INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(x_user_id,path,position)) WITHOUT ROWID;
            ''')
            db.execute('INSERT INTO metadata VALUES(1,?)', (encoded(meta),))
            def insert_details(uid, node_path, values):
                for index, item in enumerate(values):
                    row = (uid, node_path, index, encoded(item))
                    db.execute('INSERT INTO dramas VALUES(?,?,?,?)', row)
                    counts['dramas'] += 1; checksums['dramas'] ^= fingerprint(row)
            for uid, profile in sorted(data['accounts'].items()):
                summary, tree = projections(profile)
                row = (str(uid), encoded(summary), encoded(tree))
                db.execute('INSERT INTO profiles VALUES(?,?,?)', row)
                counts['profiles'] += 1; checksums['profiles'] ^= fingerprint(row)
                for i, dtype in enumerate(profile.get('types', [])):
                    for j, audience in enumerate(dtype.get('children', [])):
                        for k, theme in enumerate(audience.get('children', [])):
                            insert_details(str(uid), f'$.types[{i}].children[{j}].children[{k}].dramas', theme.get('dramas', []))
                for i, tag in enumerate(profile.get('tags', [])):
                    insert_details(str(uid), f'$.tags[{i}].dramas', tag.get('dramas', []))
            db.commit()
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('profile_disk_integrity_failed')
            for table in counts:
                count = checksum = 0
                for row in db.execute('SELECT * FROM ' + table):
                    count += 1; checksum ^= fingerprint(row)
                if count != counts[table] or checksum != checksums[table]:
                    raise RuntimeError('profile_disk_projection_mismatch')
        if path.exists():
            # Published versions are immutable. A repeated bootstrap may only
            # reuse byte-equivalent projections, never replace open readers.
            with closing(connect(path)) as db:
                if db.execute('SELECT payload FROM metadata WHERE id=1').fetchone()[0] != encoded(meta):
                    raise ValueError('profile_disk_version_conflict')
                for table in counts:
                    count = checksum = 0
                    for row in db.execute('SELECT * FROM ' + table):
                        count += 1; checksum ^= fingerprint(row)
                    if (count, checksum) != (counts[table], checksums[table]):
                        raise ValueError('profile_disk_version_conflict')
            temporary.unlink()
        else:
            temporary.replace(path)
            path.chmod(0o444)
        pointer = directory / 'current.json'
        old = json.loads(pointer.read_text()) if pointer.exists() else {}
        manifest = dict(version=version, file=path.name, previous=old.get('file'),
                        source_proof=proof, counts=counts, checksums=checksums)
        if old.get('version') == version:
            manifest['previous'] = old.get('previous')
        atomic_json(pointer, manifest)
        keep = {path.name, manifest['previous']}
        for old_path in directory.glob('*.sqlite3'):
            if re.fullmatch(VERSION_RE + r'\.sqlite3', old_path.name) and old_path.name not in keep:
                try:
                    old_path.chmod(0o644); old_path.unlink()
                except OSError:
                    print(encoded(dict(event='portrait_disk_retention_delayed', version=version)), flush=True)
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True, timeout=3)
    db.execute('PRAGMA query_only=ON')
    return db


class Store:
    def __init__(self, root=None, fallback=None):
        self.root = Path(root or os.environ.get('X_TAG_REVENUE_ROOT', DEFAULT_ROOT)) / 'serving'
        self._fallback = fallback

    def fallback(self):
        return self._fallback or SQLStore()

    def path(self, version):
        if version is None:
            version = json.loads((self.root / 'current.json').read_text())['version']
        if not re.fullmatch(VERSION_RE, version):
            raise ValueError('invalid_profile_version')
        path = (self.root / (version + '.sqlite3')).resolve()
        if path.parent != self.root.resolve():
            raise ValueError('invalid_profile_disk_path')
        return version, path

    def load(self, x_user_ids=None, *, view='tree', version=None):
        if view not in ('summary', 'tree'):
            raise ValueError('invalid_projection')
        try:
            chosen, path = self.path(version)
            with closing(connect(path)) as db:
                meta = json.loads(db.execute('SELECT payload FROM metadata WHERE id=1').fetchone()[0])
                if meta['version'] != chosen:
                    raise ValueError('profile_disk_version_mismatch')
                ids = sorted({str(v) for v in x_user_ids or [] if v})
                where = ''
                if x_user_ids is not None:
                    where = ' WHERE x_user_id IN (' + ','.join('?' for _ in ids) + ')' if ids else ' WHERE FALSE'
                profiles = {uid: json.loads(value) for uid, value in db.execute('SELECT x_user_id,' + view + ' FROM profiles' + where, ids)}
            return {'accounts': profiles}, dict(meta, read_model='verified_disk')
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            data, meta = self.fallback().load(x_user_ids, view=view, version=version)
            return data, dict(meta, read_model='ads_ai_fallback')

    def dramas(self, x_user_id, version, json_path, page=1):
        if not isinstance(page, int) or page < 1:
            raise ValueError('invalid_page')
        try:
            _, path = self.path(version)
            with closing(connect(path)) as db:
                count = db.execute('SELECT COUNT(*) FROM dramas WHERE x_user_id=? AND path=?', (str(x_user_id), json_path)).fetchone()[0]
                rows = db.execute('SELECT payload FROM dramas WHERE x_user_id=? AND path=? AND position>=? AND position<? ORDER BY position',
                                  (str(x_user_id), json_path, (page-1)*PAGE_SIZE, page*PAGE_SIZE)).fetchall()
            return dict(page=page, page_size=PAGE_SIZE, total=count, items=[json.loads(r[0]) for r in rows])
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return self.fallback().dramas(x_user_id, version, json_path, page)
