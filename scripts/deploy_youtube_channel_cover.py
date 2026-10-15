"""Scoped channel cover defaults release; retain current publishing code and data."""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.youtube_auto_publish.channel_templates import ChannelTemplateStore
import deploy_youtube_channel_templates as common

EXPECTED = {
    'features/youtube_auto_publish/channel_templates.py': 'a40a3740633b93148b5da5542d34dde40197114e9c5d15224596eb8071fcc744',
    'static/youtube-channels.html': '270bcdf134b8e3f4ea0c08b0e3583204c22dc694c64ff727c35fded12fbb8687',
    'static/youtube-channels.js': '15cbfefd634ec8ecac925f43831be280eff03af49851d50553a02f83c8c1427c',
    'static/youtube-channels.css': '98d4bb0b29adca2310b8a0a69a33fda753fd6981f8d7c58adb70a2ddc22fc273',
    'static/youtube-publish.html': '4573cf8458ae2d893983104b69cda5c05936fa1da88fff265bac3ab68a500f4d',
    'static/youtube-publish.js': '381e6923205ca67f8b35032153cd06da5f2bc08d55b500024be7f9128304439c',
}
PROTECTED = ('app.py', 'features/youtube_auto_publish/service.py',
             'features/youtube_auto_publish/runtime.py', 'features/youtube_auto_publish/worker_runtime.py',
             'scripts/youtube_auto_publish_worker.py')
WORKERS = ('youtube-auto-publish-worker.service', 'drama-youtube-publish-worker.service',
           'drama-youtube-unified-writer.service')


def prepare(stage):
    plans = {}
    for rel, expected in EXPECTED.items():
        data = (stage / rel).read_bytes()
        if rel.endswith('.py'):
            compile(data, rel, 'exec')
        paths = [common.ROOT / rel]
        if rel.startswith('static/'):
            paths.append(common.PUBLIC / Path(rel).name)
        for path in paths:
            if common.sha(path) != expected:
                raise RuntimeError('Live file drift: ' + str(path))
            plans[path] = dict(data=data, old=expected, new=common.digest(data), mode=path.stat().st_mode & 0o777)
    return plans


def protected_state():
    return {'files': {rel: common.sha(common.ROOT / rel) for rel in PROTECTED},
            'workers': {u: common.run('systemctl', 'show', u, '-p', 'MainPID', '-p', 'ActiveState') for u in WORKERS}}


def db_factory(path):
    @contextmanager
    def connect(write=False):
        c = sqlite3.connect(str(path), timeout=20)
        c.row_factory = sqlite3.Row
        try:
            if write:
                c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()
    return connect


def template_fingerprint(path):
    with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as c:
        c.execute('PRAGMA query_only=ON')
        rows = c.execute('''SELECT tenant,channel_id,title_template,description_template,comment_template,
            version,updated_by,updated_by_name,updated_at FROM youtube_channel_template ORDER BY tenant,channel_id''').fetchall()
    return {'count': len(rows), 'sha256': hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()}


def migrate(path):
    before = template_fingerprint(path)
    ChannelTemplateStore(db_factory(path))
    if template_fingerprint(path) != before:
        raise RuntimeError('Migration changed existing template fields')
    return before


def backup_database(source, target):
    with closing(sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(str(target))) as dst:
        src.execute('PRAGMA query_only=ON')
        src.backup(dst)


def rollback(backup):
    backup = backup.resolve()
    if (common.BASE / 'backups').resolve() not in backup.parents:
        raise RuntimeError('Invalid backup directory')
    manifest = json.loads((backup / 'manifest.json').read_text())
    for raw, entry in manifest['files'].items():
        path = Path(raw)
        if not (common.ROOT in path.parents or common.PUBLIC in path.parents):
            raise RuntimeError('Invalid rollback target')
        if common.sha(path) != entry['new'] or common.sha(common.saved_file(backup, path)) != entry['old']:
            raise RuntimeError('Rollback refused: later drift or invalid backup at ' + raw)
    try:
        common.run('systemctl', 'stop', common.UNIT)
        for raw, entry in manifest['files'].items():
            path = Path(raw)
            common.install(path, common.saved_file(backup, path).read_bytes(), entry['mode'])
    finally:
        common.run('systemctl', 'start', common.UNIT)
    common.healthy()
    print(json.dumps({'rollback': 'complete', 'backup': str(backup), 'database': 'retained'}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if common.run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data disk mount mismatch')
    if args.rollback:
        rollback(args.rollback)
        return
    stage = Path(__file__).resolve().parents[1]
    commit = common.run('git', '-C', str(stage), 'rev-parse', 'HEAD')
    if common.run('git', '-C', str(stage), 'status', '--porcelain'):
        raise RuntimeError('Release checkout is not clean')
    plans = prepare(stage)
    protected = protected_state()
    database = common.ROOT / 'data/drama_material_jobs.sqlite3'
    if shutil.disk_usage(common.BASE).free < database.stat().st_size * 3 + 100 * 1024 * 1024:
        raise RuntimeError('Insufficient data disk space for backup and rehearsal')
    validation = common.BASE / 'validation'
    validation.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='channel-cover-', dir=str(validation)) as tmp:
        rehearsal = Path(tmp) / 'migration.sqlite3'
        backup_database(database, rehearsal)
        migrate(rehearsal)
        migrate(rehearsal)
    if args.check:
        print(json.dumps({'preflight': 'passed', 'commit': commit, 'files': len(plans), 'migration_rehearsed': True}))
        return
    backup = common.BASE / 'backups' / ('channel-cover-' + time.strftime('%Y%m%d-%H%M%S') + '-' + commit[:12])
    backup.mkdir(parents=True)
    manifest = {'commit': commit, 'protected': protected, 'files': {}}
    for path, entry in plans.items():
        saved = common.saved_file(backup, path)
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, saved)
        if common.sha(saved) != entry['old']:
            raise RuntimeError('Backup checksum mismatch: ' + str(path))
        manifest['files'][str(path)] = {k: v for k, v in entry.items() if k != 'data'}
    backup_database(database, backup / 'before.sqlite3')
    manifest['templates'] = template_fingerprint(backup / 'before.sqlite3')
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    for path, entry in plans.items():
        if common.sha(path) != entry['old']:
            raise RuntimeError('Pre-install drift: ' + str(path))
    if protected_state() != protected:
        raise RuntimeError('Protected runtime changed before deploy')
    installed = []
    try:
        common.run('systemctl', 'stop', common.UNIT)
        try:
            migrate(database)
            for path, entry in plans.items():
                common.install(path, entry['data'], entry['mode'])
                installed.append(path)
                if common.sha(path) != entry['new']:
                    raise RuntimeError('Installed checksum mismatch')
        except BaseException:
            for path in reversed(installed):
                common.install(path, common.saved_file(backup, path).read_bytes(), plans[path]['mode'])
            raise
    finally:
        common.run('systemctl', 'start', common.UNIT)
    try:
        common.healthy()
    except BaseException:
        rollback(backup)
        raise
    if protected_state() != protected:
        raise RuntimeError('Protected runtime changed during deploy; inspect manifest')
    print(json.dumps({'deployed': commit, 'backup': str(backup), 'files': len(plans),
                      'api': common.run('systemctl', 'is-active', common.UNIT),
                      'publisher_state': 'unchanged', 'templates': template_fingerprint(database)}))


if __name__ == '__main__':
    main()
