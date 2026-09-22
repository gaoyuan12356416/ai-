"""Hash-guarded cover provenance release; never restore publication data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import subprocess
import time

ROOT = Path('/root/drama_material_service')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
UNIT = 'youtube-auto-publish-worker.service'
FILES = {
    'features/youtube_auto_publish/cover_provenance.py': None,
    'features/youtube_auto_publish/worker_runtime.py': 'f34cd5ce93c2e6ce19f53e92d1d17778b3a8ac2745d8358bbd97ae8b735b757e',
    'features/youtube_auto_publish/runtime.py': 'd72035c51e2fa329932fa8a854a54fbfa891d73761eb7676c62d974c044363be',
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def install(source, destination):
    temp = destination.with_name(destination.name + '.provenance-new')
    shutil.copy2(source, temp)
    os.replace(temp, destination)


def idle():
    with sqlite3.connect('file:' + str(ROOT / 'data/drama_material_jobs.sqlite3') + '?mode=ro', uri=True) as c:
        assert c.execute("select count(*) from youtube_auto_preparation where lease_until>strftime('%s','now')").fetchone()[0] == 0, 'Preparation active; retry when idle'
        assert c.execute("select count(*) from drama_youtube_publish where lease_owner<>'' and lease_expires_at_utc>strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()[0] == 0, 'Publication active; retry when idle'


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(); parser.add_argument('--rollback', type=Path); args = parser.parse_args()
    assert run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 1024 ** 3
    idle()
    if args.rollback:
        backup = args.rollback.resolve(); assert (BASE / 'backups').resolve() in backup.parents
        manifest = json.loads((backup / 'manifest.json').read_text())
        assert set(manifest['files']) == set(FILES)
        for rel, entry in manifest['files'].items():
            assert sha(ROOT / rel) == entry['new'], rel
            if entry['old']: assert sha(backup / rel) == entry['old']
        run('systemctl', 'stop', UNIT)
        try:
            idle()
            for rel, entry in reversed(list(manifest['files'].items())):
                if entry['old']: install(backup / rel, ROOT / rel)
                else: (ROOT / rel).unlink()
        finally: run('systemctl', 'start', UNIT)
        print('Code rolled back; current database and assets retained')
        return
    stage = Path(__file__).resolve().parents[1]
    commit = run('git', '-C', str(stage), 'rev-parse', 'HEAD')
    assert not run('git', '-C', str(stage), 'status', '--porcelain')
    for rel, expected in FILES.items():
        assert sha(ROOT / rel) == expected, rel
        py_compile.compile(str(stage / rel), doraise=True)
    backup = BASE / 'backups' / ('cover-provenance-' + time.strftime('%Y%m%d-%H%M%S') + '-' + commit[:12])
    backup.mkdir(parents=True)
    manifest = {'commit': commit, 'files': {}}
    for rel, expected in FILES.items():
        if expected:
            dest = backup / rel; dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dest); assert sha(dest) == expected
        manifest['files'][rel] = {'old': expected, 'new': sha(stage / rel)}
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    db = ROOT / 'data/drama_material_jobs.sqlite3'
    with sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True) as src, sqlite3.connect(str(backup / 'before.sqlite3')) as dst:
        src.backup(dst)
    run('systemctl', 'stop', UNIT)
    try:
        idle()
        for rel in FILES:
            assert sha(ROOT / rel) == FILES[rel], rel
            install(stage / rel, ROOT / rel)
        for rel, entry in manifest['files'].items(): assert sha(ROOT / rel) == entry['new'], rel
    finally: run('systemctl', 'start', UNIT)
    print(json.dumps({'backup': str(backup), 'commit': commit, 'state': run('systemctl', 'is-active', UNIT)}))


if __name__ == '__main__': main()
