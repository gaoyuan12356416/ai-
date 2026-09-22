"""Exact GitHub release, additive storage and narrow API restart; no test publishing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
UNIT = 'drama-material-api.service'
EXPECTED = {
    'app.py': '26bd03593246aa16b85f4d43be1dcdd1e1fc12bdbf16f5b0f62a5dc3a0fde374',
    'features/youtube_auto_publish/service.py': 'a2582f60512162a1406cd2c426f0fa76ab9148f291bfdc21f9e1e2765984ed1a',
    'features/youtube_auto_publish/channels.py': '2e61d01f037e06b98fac1cc4ac415caa7157a2b6d5f8c5b01c8e827bfcd95784',
    'features/youtube_auto_publish/channel_templates.py': None,
    'static/youtube-publish.js': 'a870705f1f21314fdd08317b3bf3bfe1a478aade326e9b31eb0ce2bee6bdfd42',
    'static/youtube-publish.html': 'b1a17d21ba3213b6731aefc82e17f165cd5bd27c4733a89b5984734ac34245ce',
    'static/youtube-channels.js': None,
    'static/youtube-channels.html': None,
    'static/youtube-channels.css': None,
}
# Kept separate: shared navigation has valid live-only patches. Append our item only.
QUICK_NAV_SHA = '96f9e784fc7268678f8c40156ad274dfc3868e8685537d94c78dfeaaca4db5bf'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha(path):
    return digest(path.read_bytes()) if path.exists() else None


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def nav_script(current, source):
    marker = '          key: "youtubeChannelList",'
    if marker in current:
        raise RuntimeError('Channel navigation already exists; inspect rather than overwrite')
    start = source.rfind('        {', 0, source.index(marker))
    end = source.index('        },', source.index(marker)) + len('        },\n')
    index = current.index('      ],', current.index('          key: "youtubeAutoPublish",'))
    return current[:index] + source[start:end] + current[index:]


def nav_config(current):
    value = json.loads(current)
    group = next(g for g in value if g['key'] == 'youtube_platform')
    if any(i.get('key') == 'youtubeChannelList' for i in group['items']):
        raise RuntimeError('Channel navigation already exists')
    # Preserve group and item permission metadata, including future custom restrictions.
    item = dict(next(i for i in group['items'] if i['key'] == 'youtubeAutoPublish'))
    item.update(key='youtubeChannelList', label='YouTube 频道列表',
                description='查看频道信息并配置共用发布文案模板', href='/youtube-channels.html', order=20)
    group['items'].append(item)
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def prepare(stage):
    plans = {}
    def add(path, data, expected):
        if sha(path) != expected:
            raise RuntimeError('Live file drift: ' + str(path))
        plans[path] = dict(data=data, old=expected, new=digest(data), mode=(path.stat().st_mode & 0o777) if path.exists() else 0o644)
    for rel, expected in EXPECTED.items():
        data = (stage / rel).read_bytes()
        if rel.endswith('.py'):
            compile(data, rel, 'exec')
        add(ROOT / rel, data, expected)
        if rel.startswith('static/'):
            add(PUBLIC / Path(rel).name, data, expected)
    for path in (ROOT / 'static/quick-nav.js', PUBLIC / 'quick-nav.js'):
        add(path, nav_script(path.read_text(encoding='utf-8'), (stage/'static/quick-nav.js').read_text(encoding='utf-8')).encode(), QUICK_NAV_SHA)
    for path in (ROOT / 'static/navigation.json', PUBLIC / 'navigation.json'):
        before = path.read_bytes()
        add(path, nav_config(before).encode(), digest(before))
    return plans


def saved_file(backup, path):
    return backup/'files'/path.relative_to(path.anchor)


def install(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.channel-template-new')
    temp.write_bytes(data)
    os.chmod(temp, mode)
    os.replace(temp, path)


def healthy():
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status == 200 and isinstance(json.load(response), dict):
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('API health check failed')


def rollback(backup):
    backup = backup.resolve()
    if (BASE/'backups').resolve() not in backup.parents:
        raise RuntimeError('Invalid backup directory')
    manifest = json.loads((backup/'manifest.json').read_text())
    for raw, entry in manifest['files'].items():
        path = Path(raw)
        if not (ROOT in path.parents or PUBLIC in path.parents):
            raise RuntimeError('Invalid rollback target')
        if sha(path) != entry['new']:
            raise RuntimeError('Rollback refused: later file drift at ' + raw)
        if entry['old'] and sha(saved_file(backup, path)) != entry['old']:
            raise RuntimeError('Backup checksum mismatch')
    run('systemctl', 'stop', UNIT)
    try:
        for raw, entry in reversed(list(manifest['files'].items())):
            path = Path(raw)
            if entry['old']:
                install(path, (saved_file(backup, path)).read_bytes(), entry['mode'])
            else:
                path.unlink()
    finally:
        run('systemctl', 'start', UNIT)
    healthy()
    print(json.dumps({'rollback':'complete','backup':str(backup),'database':'retained'}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if run('findmnt','-n','-o','UUID','/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data disk mount mismatch')
    if args.rollback:
        rollback(args.rollback)
        return
    stage = Path(__file__).resolve().parents[1]
    commit = run('git','-C',str(stage),'rev-parse','HEAD')
    if run('git','-C',str(stage),'status','--porcelain'):
        raise RuntimeError('Release checkout is not clean')
    plans = prepare(stage)
    if args.check:
        print(json.dumps({'preflight':'passed','commit':commit,'files':len(plans)}))
        return
    backup = BASE/'backups'/('channel-templates-'+time.strftime('%Y%m%d-%H%M%S')+'-'+commit[:12])
    backup.mkdir(parents=True)
    manifest = {'commit':commit,'files':{}}
    for path, entry in plans.items():
        if entry['old']:
            saved = saved_file(backup, path)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
            if sha(saved) != entry['old']:
                raise RuntimeError('Backup drift: ' + str(path))
        manifest['files'][str(path)] = {k:v for k,v in entry.items() if k != 'data'}
    (backup/'manifest.json').write_text(json.dumps(manifest, indent=2))
    db = ROOT/'data/drama_material_jobs.sqlite3'
    with sqlite3.connect('file:'+str(db)+'?mode=ro', uri=True) as src, sqlite3.connect(str(backup/'before.sqlite3')) as dst:
        src.backup(dst)
    # Final compare immediately before narrowing the API outage window.
    for path, entry in plans.items():
        if sha(path) != entry['old']:
            raise RuntimeError('Pre-install drift: ' + str(path))
    run('systemctl','stop',UNIT)
    installed = []
    try:
        for path, entry in plans.items():
            install(path, entry['data'], entry['mode'])
            installed.append(path)
            if sha(path) != entry['new']:
                raise RuntimeError('Installed checksum mismatch')
    except BaseException:
        for path in reversed(installed):
            entry = plans[path]
            if entry['old']:
                install(path, (saved_file(backup, path)).read_bytes(), entry['mode'])
            else:
                path.unlink()
        raise
    finally:
        run('systemctl','start',UNIT)
    healthy()
    print(json.dumps({'deployed':commit,'backup':str(backup),'files':len(plans),'api':run('systemctl','is-active',UNIT)}))


if __name__ == '__main__':
    main()
