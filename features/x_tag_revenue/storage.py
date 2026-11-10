"""Versioned ads_ai portraits. Readers fetch projections, never cache the full catalog."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import zlib

from features.social_analytics.warehouse_refresh import Reader, Writer, environment

TABLE = 'ads_ai.x_channel_revenue_profile'
PAGE_SIZE = 50
VERSION_RE = r'[0-9]{8}T[0-9]{6,12}Z'
DDL = '''CREATE TABLE IF NOT EXISTS ads_ai.x_channel_revenue_profile (
  profile_version VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  x_user_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  summary_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  tree_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  detail_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  revenue_cents BIGINT NOT NULL DEFAULT 0,
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (profile_version, x_user_id)
) ENGINE=InnoDB COMMENT='X channel revenue portraits; derived versioned projections' '''


class SnapshotExpired(ValueError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def literal(value):
    return '0x' + str(value).encode('utf-8').hex() if str(value) else "''"


def runtime_env():
    env = dict(os.environ)
    for prefix in ('ADMIN_MAPPING_MYSQL_', 'X_AUTO_POST_MYSQL_', 'X_POST_DAILY_MYSQL_'):
        if all(env.get(prefix + key) for key in ('HOST', 'PORT', 'USER', 'PASSWORD')):
            env.update({'ADMIN_MAPPING_MYSQL_' + key: env[prefix + key] for key in ('HOST', 'PORT', 'USER', 'PASSWORD')})
            return env
    path = Path(os.environ.get('X_TAG_PROFILE_ENV', '/root/drama_material_service/.env'))
    return environment(path) if path.exists() else env


def tree_summary(nodes):
    return [{k: (tree_summary(v) if k == 'children' else v)
             for k, v in row.items() if k != 'dramas'} for row in nodes]


def projections(portrait):
    root = 'audiences' if 'audiences' in portrait else 'types'
    tree = {k: v for k, v in portrait.items() if k not in ('types', 'audiences', 'tags')}
    tree[root] = tree_summary(portrait.get(root, []))
    tree['tags'] = [{k: v for k, v in t.items() if k != 'dramas'} for t in portrait.get('tags', [])]
    summary = {k: v for k, v in tree.items() if k not in ('types', 'audiences', 'tags', 'combinations')}
    summary[root] = [{k: v for k, v in t.items() if k != 'children'} for t in tree[root]]
    summary['combinations'] = tree.get('combinations', [])[:3]
    summary['tag_count'] = len(tree['tags'])
    summary['tags'] = tree['tags'][:3]
    # Each displayed layer has an explicit parent. Never pool audience/theme
    # shares across types, and never choose a different type via a top leaf.
    if root == 'audiences':
        audiences = tree[root]
        audience = max(audiences, key=lambda t: t['revenue_cents']) if audiences else {}
        simple = lambda n: {k: v for k, v in n.items() if k != 'children'}
        summary['focus'] = dict(audience=simple(audience),
                                themes=[simple(n) for n in audience.get('children', [])[:3]])
        return summary, tree
    types = tree['types']
    primary = max(types, key=lambda t: t['revenue_cents']) if types else {}
    audiences = primary.get('children', [])
    audience = max(audiences, key=lambda t: t['revenue_cents']) if audiences else {}
    simple = lambda n: {k: v for k, v in n.items() if k != 'children'}
    summary['focus'] = dict(type=simple(primary), audience=simple(audience),
                            audiences=[simple(n) for n in audiences],
                            themes=[simple(n) for n in audience.get('children', [])[:2]])
    return summary, tree


class Store:
    def __init__(self, env=None, reader=None):
        self.reader = reader or Reader(env if env is not None else runtime_env())

    def load(self, x_user_ids=None, *, view='tree', version=None):
        if view not in ('summary', 'tree'):
            raise ValueError('invalid_projection')
        if version is not None and not re.fullmatch(VERSION_RE, version):
            raise ValueError('invalid_profile_version')
        chosen = literal(version) if version else ("(SELECT JSON_UNQUOTE(JSON_EXTRACT(summary_json,'$.version')) "
                    f"FROM {TABLE} WHERE profile_version='@current' AND x_user_id='')")
        ids = sorted(set(str(x) for x in x_user_ids or [] if x))
        scope = ("x_user_id IN (" + ','.join(map(literal, ids)) + ')') if ids else 'FALSE'
        if x_user_ids is None:
            scope = "x_user_id<>''"
        rows = self.reader.query([
            'SET @xtr_version=' + chosen,
            f"SELECT HEX(JSON_OBJECT('meta',JSON_EXTRACT(summary_json,'$'))) FROM {TABLE} "
            "WHERE profile_version=@xtr_version AND x_user_id=''",
            f"SELECT HEX(JSON_OBJECT('x_user_id',x_user_id,'profile',JSON_EXTRACT({view}_json,'$'))) "
            f"FROM {TABLE} WHERE profile_version=@xtr_version AND {scope}",
        ], timeout=20)
        metadata = next((r['meta'] for r in rows if 'meta' in r), None)
        if metadata is None:
            if version:
                raise SnapshotExpired('portrait_version_expired')
            raise RuntimeError('portrait_database_unavailable')
        return {'accounts': {r['x_user_id']: r['profile'] for r in rows if 'x_user_id' in r}}, metadata

    def dramas(self, x_user_id, version, json_path, page=1):
        if not re.fullmatch(VERSION_RE, version) or not re.fullmatch(r'\$\.(?:audiences|types|tags)\[\d+\](?:\.children\[\d+\]){0,2}\.dramas', json_path):
            raise ValueError('invalid_detail_path')
        offset = (page - 1) * PAGE_SIZE
        # Multiple paths in ONE extraction parse the channel JSON once. Calling
        # JSON_EXTRACT 50 times made a real detail page take about 14 seconds.
        paths = ','.join(f"'{json_path}[{i}]'" for i in range(offset, offset + PAGE_SIZE))
        rows = self.reader.query([f"SELECT HEX(JSON_OBJECT('total',JSON_LENGTH(JSON_EXTRACT(detail_json,'{json_path}')),"
                  f"'items',JSON_EXTRACT(detail_json,{paths}))) FROM {TABLE} WHERE profile_version={literal(version)} "
                  f"AND x_user_id={literal(x_user_id)}"], timeout=20)
        if not rows:
            raise SnapshotExpired('portrait_version_expired')
        return dict(page=page, page_size=PAGE_SIZE, total=rows[0]['total'] or 0,
                    items=[r for r in rows[0]['items'] or [] if r is not None])


class ProfileWriter(Writer):
    """Reuses the warehouse connection boundary; writes only this derived table."""
    def install_schema(self):
        with self.db.cursor() as cursor:
            cursor.execute(DDL)
        self.db.commit()

    def publish(self, data, reader):
        version = data['meta']['version']
        if not re.fullmatch(VERSION_RE, version):
            raise ValueError('invalid_profile_version')
        with self.db.cursor() as cursor:
            cursor.execute(f"SELECT summary_json FROM {TABLE} WHERE profile_version='@current' AND x_user_id=''")
            row = cursor.fetchone()
        head = json.loads(row[0]) if row else {}
        previous = head.get('previous') if head.get('version') == version else head.get('version')
        sql = (f'INSERT INTO {TABLE}(profile_version,x_user_id,summary_json,tree_json,detail_json,revenue_cents) '
               'VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE '
               'summary_json=VALUES(summary_json),tree_json=VALUES(tree_json),detail_json=VALUES(detail_json),'
               'revenue_cents=VALUES(revenue_cents),updated_at=CURRENT_TIMESTAMP(6)')
        checksum, count, revenue, batch, size = 0, 0, 0, [], 0
        meta = dict(data['meta'], storage='ads_ai.x_channel_revenue_profile', audit=data.get('audit', {}))
        def rows():
            yield version, '', encoded(meta), '{}', '{}', 0
            for uid, profile in sorted(data['accounts'].items()):
                if not re.fullmatch(r'[0-9]{1,32}', str(uid)):
                    raise ValueError('invalid_x_user_id')
                summary, tree = projections(profile)
                yield version, str(uid), encoded(summary), encoded(tree), encoded(profile), profile['revenue_cents']
        for row in rows():
            payload = '\n'.join(row[2:5])
            checksum ^= zlib.crc32((row[1] + ':' + hashlib.sha256(payload.encode('utf-8')).hexdigest()).encode('ascii'))
            count += 1
            revenue += row[5]
            # Active versions are immutable, including deployment retries.
            # Re-verify them without rewriting tens of MB of identical rows.
            if head.get('version') != version:
                batch.append(row)
            size += len(payload.encode('utf-8'))
            if batch and (size >= 1024 * 1024 or len(batch) >= 50):
                self.write_batch(sql, batch)
                batch, size = [], 0
        if batch:
            self.write_batch(sql, batch)
        # Verify every stored byte and the revenue aggregate on the read replica
        # before publishing the head. Incomplete generations are never served.
        expected = dict(rows=count, revenue=revenue, checksum=checksum)
        query = ("SELECT HEX(JSON_OBJECT('rows',COUNT(*),'revenue',COALESCE(SUM(revenue_cents),0),"
                 "'checksum',BIT_XOR(CRC32(CONCAT(x_user_id,':',SHA2(CONCAT(summary_json,CHAR(10),tree_json,CHAR(10),detail_json),256)))))) "
                 f'FROM {TABLE} WHERE profile_version={literal(version)}')
        deadline = time.monotonic() + 90
        while True:
            actual = reader.query([query], timeout=30)[0]
            if all(int(actual[k]) == v for k, v in expected.items()):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('profile_replica_verification_failed')
            time.sleep(3)
        if head.get('version') == version:
            return dict(version=version, **expected)
        self.write_batch(sql, [('@current', '', encoded({'version': version, 'previous': previous}), '{}', '{}', 0)])
        # Retain the active generation and its predecessor for open dialogs and
        # rollback. This table is derived data, never a publication ledger.
        try:
            with self.db.cursor() as cursor:
                cursor.execute(f"SELECT profile_version FROM {TABLE} WHERE x_user_id='' AND profile_version<>'@current' ORDER BY profile_version DESC")
                obsolete = [r[0] for r in cursor.fetchall() if r[0] not in (version, previous)]
            for old in obsolete:
                with self.db.cursor() as cursor:
                    cursor.execute(f'DELETE FROM {TABLE} WHERE profile_version=%s', (old,))
                self.db.commit()
        except Exception:
            # A retention failure must not misreport a verified publication as
            # failed. The next hourly refresh retries cleanup of old versions.
            print(encoded(dict(event='portrait_retention_delayed', version=version)), flush=True)
        return dict(version=version, **expected)
