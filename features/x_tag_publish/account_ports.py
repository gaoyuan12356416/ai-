"""List the currently enabled publishing configurations for visible X accounts."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def template_rows(path):
    with closing(readonly(path)) as db:
        rows = db.execute("""
            SELECT t.id, t.name, v.config_json
            FROM x_auto_template AS t LEFT JOIN x_auto_template_version AS v
              ON v.template_id = t.id AND v.version = t.current_version
            WHERE t.enabled = 1 AND t.deleted_at_utc = '' ORDER BY t.id
        """)
        result = []
        for row in rows:
            try:
                ids = json.loads(row['config_json'])['account_ids']
            except (ValueError, TypeError, KeyError):
                ids = None
            result.append([row['id'], row['name'], ids])
        return result


def owner_template_rows(path):
    # A read-only connection can create WAL sidecars. Keep them owned by Auto.
    process = subprocess.run(['/usr/sbin/runuser', '-u', 'x-post-daily', '--', '/usr/bin/python3', '-B',
                              str(Path(__file__).resolve()), str(path)],
                             capture_output=True, text=True, timeout=15, check=True,
                             env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    return json.loads(process.stdout)


def account_publishing_ports(accounts, templates_db, tag_pool_db,
                             personal_db='/var/lib/x-post-automation/accounts.sqlite3',
                             read_templates=owner_template_rows):
    items = {str(account['id']): [] for account in accounts}
    unavailable = set()

    def add(account_ids, key, label):
        if not isinstance(account_ids, list):
            raise ValueError('invalid account list')
        for account_id in {str(value) for value in account_ids}:
            if account_id in items:
                items[account_id].append({'key': key, 'label': label})

    try:
        for template_id, name, ids in read_templates(templates_db):
            if isinstance(ids, list):
                add(ids, 'template:' + str(template_id), name)
            else:
                unavailable.add('自动发布模板')
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        unavailable.add('自动发布模板')

    try:
        with closing(readonly(tag_pool_db)) as db:
            row = db.execute("SELECT enabled, account_ids_json FROM x_post_schedule_config "
                             "WHERE source_type = 'material'").fetchone()
            if row is None:
                unavailable.add('标签画像素材池')
            elif row['enabled']:
                add(json.loads(row['account_ids_json']), 'tag_material_pool', '标签画像素材池')
    except (OSError, sqlite3.Error, TypeError, ValueError):
        unavailable.add('标签画像素材池')

    for account in accounts:
        sources = account.get('auto_publish_sources')
        if not isinstance(sources, list):
            unavailable.add('短剧池')
        elif 'drama' in sources:
            items[str(account['id'])].append({'key': 'drama_pool', 'label': '短剧池'})

    try:
        with closing(readonly(personal_db)) as db:
            rows = db.execute("SELECT DISTINCT account_id FROM x_video_publish_pool WHERE status = 'enabled'")
            add([row['account_id'] for row in rows], 'personal_pool', '账号素材池')
    except (OSError, sqlite3.Error):
        unavailable.add('账号素材池')

    return {'items': items, 'unavailable': sorted(unavailable)}


if __name__ == '__main__':
    print(json.dumps(template_rows(sys.argv[1]), ensure_ascii=True, separators=(',', ':')))
