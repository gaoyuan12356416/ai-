"""Install bounded independent preparation dispatch, preserving live ledgers."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import subprocess


ROOT = Path('/root/drama_material_service')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
UNIT = 'youtube-auto-publish-worker.service'
FILES = {
    'features/youtube_auto_publish/worker_runtime.py': '541baeb3c88eca5c9c4b5e6b13f7ba554722dcc1138832125fe609a5198b3418',
    'scripts/youtube_auto_publish_worker.py': 'e9429284e7e87f537f4cd443a9337263ff4dd44d3345243bca8986267e5e2b74',
}


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(source, destination):
    temporary = destination.with_name(destination.name + '.throughput-new')
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def drain():
    pid = int(run('systemctl', 'show', UNIT, '-p', 'MainPID', '--value'))
    assert pid > 0
    # Finish the current publication and generation. A systemctl restart would
    # enforce the unit's 60-second kill timeout on potentially ambiguous writes.
    run('systemctl', 'kill', '--kill-who=main', '--signal=SIGTERM', UNIT)
    return pid


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    assert run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert run('systemctl', 'show', UNIT, '-p', 'Restart', '--value') == 'always'
    assert run('systemctl', 'is-active', UNIT) == 'active'
    if args.rollback:
        backup = args.rollback.resolve()
        assert (BASE/'backups').resolve() in backup.parents
        manifest = json.loads((backup/'manifest.json').read_text())
        assert set(manifest['files']) == set(FILES)
        for rel, entry in manifest['files'].items():
            assert sha(ROOT/rel) == entry['new'], rel
            assert sha(backup/rel) == entry['old'], rel
        for rel in reversed(list(FILES)):
            install(backup/rel, ROOT/rel)
        print(json.dumps({'rollback': str(backup), 'draining_pid': drain(),
                          'database_and_assets': 'retained'}))
        return
    stage = Path(__file__).resolve().parents[1]
    commit = run('git', '-C', str(stage), 'rev-parse', 'HEAD')
    assert not run('git', '-C', str(stage), 'status', '--porcelain')
    assert shutil.disk_usage('/mnt/data-disk').free > 1024**3
    for rel, expected in FILES.items():
        assert sha(ROOT/rel) == expected, rel
        py_compile.compile(str(stage/rel), doraise=True)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    backup = BASE/'backups'/('cover-throughput-'+stamp+'-'+commit[:12])
    backup.mkdir(parents=True)
    manifest = {'commit': commit, 'files': {}, 'units': {}}
    for unit in (UNIT, 'drama-material-api.service', 'drama-youtube-unified-writer.service'):
        manifest['units'][unit] = run('systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ActiveState')
    for rel, expected in FILES.items():
        target = backup/rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/rel, target)
        assert sha(target) == expected
        manifest['files'][rel] = {'old': expected, 'new': sha(stage/rel)}
    with closing(sqlite3.connect('file:'+str(ROOT/'data/drama_material_jobs.sqlite3')+'?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(str(backup/'before.sqlite3'))) as dst:
            src.backup(dst)
    (backup/'manifest.json').write_text(json.dumps(manifest, indent=2))
    for rel, expected in FILES.items():
        assert sha(ROOT/rel) == expected, rel
        install(stage/rel, ROOT/rel)
    for rel, entry in manifest['files'].items():
        assert sha(ROOT/rel) == entry['new'], rel
        py_compile.compile(str(ROOT/rel), doraise=True)
    print(json.dumps({'backup': str(backup), 'commit': commit, 'draining_pid': drain()}))


if __name__ == '__main__':
    main()
