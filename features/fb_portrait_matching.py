"""Read Page matching fields from the verified projection of the current MySQL table."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3


TABLE = 'ads_ai.fb_page_revenue_profile'
VERSION = re.compile(r'[0-9]{8}T[0-9]{12}Z')


def from_mysql(pointer):
    return pointer.get('backend') == 'mysql' and pointer.get('database') == TABLE


def read_matching(root, pointer, page_ids):
    hint = pointer['version']
    if not from_mysql(pointer) or not isinstance(hint, str) or not VERSION.fullmatch(hint):
        raise ValueError('invalid_matching_pointer')
    path = Path(root).resolve() / 'matching' / 'current.sqlite3'
    ids = list(dict.fromkeys(str(pid) for pid in page_ids))
    found, errors = {}, {}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=3)) as db:
        db.execute('BEGIN')
        head = db.execute('SELECT payload FROM head').fetchone()
        meta = json.loads(head[0]) if head else {}
        version = meta.get('version')
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            raise ValueError('invalid_matching_version')
        if version < hint:
            meta['stale'] = True
        for offset in range(0, len(ids), 400):
            batch = ids[offset:offset + 400]
            rows = db.execute('SELECT page_id,projection,revenue_cents,hierarchy_version FROM pages '
                              'WHERE page_id IN (' + ','.join('?' for _ in batch) + ')', batch)
            for pid, raw, cents, hierarchy in rows:
                try:
                    page = json.loads(raw)
                    if not isinstance(page, dict) or page.get('page_id') != pid:
                        raise ValueError('wrong Page')
                    page.update(revenue_cents=cents, hierarchy_version=hierarchy)
                    found[pid] = page
                except (ValueError, TypeError):
                    errors[pid] = 'portrait_invalid'
    return found, meta, errors


def recovery_profiles(ids, root='/mnt/data-disk/fb-page-portraits'):
    try:
        pointer = json.loads((Path(root) / 'current.json').read_text(encoding='utf-8'))
        if from_mysql(pointer):
            pages, meta, errors = read_matching(root, pointer, ids)
            return pages, meta, ['profile_invalid:' + pid for pid in errors]
        from features.fb_page_assignment.sources import load_profiles
        return load_profiles(ids, root=root)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        return {}, {}, ['profile_snapshot_unavailable']
