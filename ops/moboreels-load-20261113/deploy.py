"""Activate this exact, previously fetched GitHub release; preserve business ledgers."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    release = Path(__file__).resolve().parents[2]
    sha = subprocess.check_output(['git', '-C', str(release), 'rev-parse', 'HEAD'], text=True).strip()
    assert len(sys.argv) == 2 and sha == sys.argv[1], 'exact release required'
    assert subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk'], text=True).strip() == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    current = Path('/opt/moboreels-library/current')
    previous = current.resolve()
    assert previous.name == 'f787e620a2581b6b7dfceb76b11422231dec1893', 'live release changed'
    base = Path('/mnt/data-disk/moboreels-library')
    changed = ['features/moboreels_library/catalog.py', 'features/moboreels_library/service.py']
    mappings = [(release / name, Path('/root/drama_material_service') / name, previous / name) for name in changed]
    for directory in ['/root/drama_material_service/static', '/usr/share/nginx/html']:
        for name in ['moboreels-library.js', 'moboreels-library.css', 'moboreels-library.html']:
            mappings.append((release / 'static' / name, Path(directory) / name, previous / 'static' / name))
    mappings.append((release / 'deploy/nginx/moboreels-library.conf', Path('/etc/nginx/default.d/moboreels-library.conf'), previous / 'deploy/nginx/moboreels-library.conf'))
    for source, target, baseline in mappings:
        assert target.read_bytes() == baseline.read_bytes(), 'live file changed: ' + str(target)
    run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_moboreels*.py', '-q', cwd=str(release))
    for name, table, column in [('sync.sqlite3', 'sync_run', 'payload'), ('promotions.sqlite3', 'promotions', 'record_json')]:
        with sqlite3.connect(str(base / 'data' / name)) as conn:
            assert not any(json.loads(r[0]).get('status') in ('running', 'queued', 'creating') for r in conn.execute('SELECT '+column+' FROM '+table)), 'active work; defer restart'
    backup = base / 'deployments' / ('load-' + datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    manifest = {'commit': sha, 'previous_current': str(previous), 'files': []}
    for index, (source, target, baseline) in enumerate(mappings):
        saved = backup / ('file-%02d' % index)
        shutil.copy2(str(target), str(saved))
        manifest['files'].append({'target': str(target), 'backup': str(saved), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
    for name in ['sync.sqlite3', 'promotions.sqlite3']:
        with sqlite3.connect(str(base/'data'/name)) as src, sqlite3.connect(str(backup/name)) as dst:
            src.backup(dst)
    (backup/'manifest.json').write_text(json.dumps(manifest, indent=2))
    shutil.copy2(str(release/'ops/moboreels-load-20261113/rollback.py'), str(backup/'rollback.py'))
    for source, target, baseline in mappings:
        staged = target.with_name(target.name+'.moboreels-next')
        shutil.copy2(str(source), str(staged)); os.replace(str(staged), str(target))
    try:
        run('nginx', '-t')
        staged = current.with_name('load-next'); staged.symlink_to(release); os.replace(str(staged), str(current))
        run('systemctl', 'restart', 'moboreels-library.service')
        run('systemctl', 'reload', 'nginx')
        run('systemctl', 'is-active', 'moboreels-library.service')
    except Exception:
        run(sys.executable, str(backup/'rollback.py'))
        raise
    print(json.dumps({'commit': sha, 'backup': str(backup), 'files': len(mappings)}))


if __name__ == '__main__':
    main()
