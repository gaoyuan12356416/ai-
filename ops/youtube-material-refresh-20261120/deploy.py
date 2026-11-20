"""SQL/static hot update and scoped rollback for the material picker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
DATA = Path('/mnt/data-disk/youtube-material-refresh-20261120')
BACKUP = DATA / 'backup'
API = 'drama-material-api.service'
BASELINES = {
    'features/youtube_auto_publish/source.py': '658f86696ae12ac528a581a0a364d5c7e81527afce628e7e52f10be5deb72827',
    'static/youtube-publish.js': 'f7657859b7461de520c62bd74f889892f63ac5a6894a4af389ea6046e15ec826',
    'static/youtube-publish.html': 'b10a182345aec1c1336e151e59dbd983a8501e1c50608428a55c5a8dd154edd8',
    'ops/youtube-material-refresh-20261120/material-source.sql': '5c2a7cea889fb3b92043c7eef438957b118057f396a5f5dd23543270ecf6f861',
}


def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def state():
    result = subprocess.check_output(['systemctl', 'show', API, '-p', 'MainPID', '-p', 'ActiveState', '-p', 'SubState'], text=True)
    return dict(line.split('=', 1) for line in result.splitlines())


def targets():
    for name, expected in BASELINES.items():
        if name.endswith('.sql'):
            yield name, Path('/etc/youtube-auto-publish/material-source.sql'), expected
        else:
            yield name, Path('/root/drama_material_service') / name, expected
            if name.startswith('static/'):
                yield name, Path('/usr/share/nginx/html') / Path(name).name, expected


def install_bytes(source, target):
    meta = target.stat()
    fd, temporary = tempfile.mkstemp(prefix='.material-refresh-', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(source.read_bytes()); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, meta.st_mode & 0o7777)
        os.chown(temporary, meta.st_uid, meta.st_gid)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def save(record):
    (BACKUP / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'finish-install', 'rollback'))
    parser.add_argument('commit')
    args = parser.parse_args()
    assert subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip() == args.commit
    assert os.path.ismount('/mnt/data-disk')
    assert subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '--target', '/mnt/data-disk'], text=True).strip() == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    if args.action == 'install':
        assert not BACKUP.exists(), 'Existing deployment must be reconciled before retry'
        assert shutil.disk_usage(DATA).free > 1024 ** 3
        for name, target, expected in targets():
            assert digest(target) == expected, 'Live drift: ' + str(target)
            assert (ROOT / name).read_bytes() == subprocess.check_output(['git', '-C', str(ROOT), 'show', args.commit + ':' + name])
        py_compile.compile(str(ROOT / 'features/youtube_auto_publish/source.py'), doraise=True)
        BACKUP.mkdir(mode=0o700)
        record = dict(commit=args.commit, status='prepared', api_before=state(), files=[])
        for number, (name, target, expected) in enumerate(targets()):
            backup = BACKUP / str(number)
            shutil.copy2(target, backup)
            assert digest(backup) == expected
            record['files'].append(dict(source=name, target=str(target), backup=str(backup), before=expected, after=digest(ROOT / name)))
        save(record)
        for row in record['files']:
            install_bytes(ROOT / row['source'], Path(row['target']))
            assert digest(row['target']) == row['after']
        record.update(status='active', installed_at=time.time(), activation='sql_static_hot_reload', api_after=state())
        save(record)
        print(json.dumps({'status': 'active', 'backup': str(BACKUP), 'api': state()}))
        return
    record = json.loads((BACKUP / 'manifest.json').read_text())
    if args.action == 'finish-install':
        assert record['commit'] == '99a54c8c8c9b1a6d93f174694cd5358574332e61'
        assert record['status'] in ('installed', 'active'), 'Reconcile deployment before proceeding'
        subprocess.run(['systemctl', 'stop', 'youtube-material-refresh-activate-20261120.service'], check=True)
        # Preserve the original backup; audit and fence every already installed file.
        for row in record['files']:
            assert digest(row['target']) == row['after'], 'Later drift: ' + row['target']
            source = ROOT / row['source']
            assert source.read_bytes() == subprocess.check_output(['git', '-C', str(ROOT), 'show', args.commit + ':' + row['source']])
        (BACKUP / 'previous-manifest.json').write_text(json.dumps(record, indent=2) + '\n')
        for row in record['files']:
            source = ROOT / row['source']
            if digest(source) != row['after']: install_bytes(source, Path(row['target']))
            row['after'] = digest(source)
            assert digest(row['target']) == row['after']
        record.update(commit=args.commit, status='active', activation='sql_static_hot_reload', api_after=state())
        save(record)
        print(json.dumps({'status': 'active', 'backup': str(BACKUP), 'api': state()}))
    else:
        assert record['commit'] == args.commit
        for row in record['files']:
            assert digest(row['target']) == row['after'], 'Later drift: ' + row['target']
            assert digest(row['backup']) == row['before']
        for row in record['files']: install_bytes(Path(row['backup']), Path(row['target']))
        record['status'] = 'rolled_back'
        save(record)
        print(json.dumps({'status': 'rolled_back', 'api': state()}))


if __name__ == '__main__': main()
