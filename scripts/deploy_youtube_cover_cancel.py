"""Deploy cancellation-aware generation; drain current work without killing it."""
import argparse
from contextlib import closing
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
    'features/youtube_auto_publish/worker_runtime.py': 'b8be0f259e4c73ce0fb0b97a76121cb90c27ecdb621587aa2220505e364b9263',
    'features/youtube_auto_publish/runtime.py': 'b6a5bced17b7a7c3488c53320a3e53f2a6400519c01670a06b682edc90d15129',
}


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(source, destination):
    temporary = destination.with_name(destination.name + '.cancel-new')
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def signal_drain():
    # The existing worker finishes its publication turn and pending preparation
    # before exit. Avoid systemctl stop's 60-second forced-kill deadline.
    pid = int(run('systemctl', 'show', UNIT, '-p', 'MainPID', '--value'))
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
        # The old adapter does not pass a cancellation callback, so restore it
        # before removing callback support from worker_runtime.
        for rel in reversed(list(FILES)):
            install(backup/rel, ROOT/rel)
        pid = signal_drain()
        print(json.dumps({'rollback': str(backup), 'draining_pid': pid,
                          'database_and_assets': 'retained'}))
        return
    stage = Path(__file__).resolve().parents[1]
    commit = run('git', '-C', str(stage), 'rev-parse', 'HEAD')
    assert not run('git', '-C', str(stage), 'status', '--porcelain')
    assert shutil.disk_usage('/mnt/data-disk').free > 1024**3
    for rel, expected in FILES.items():
        assert sha(ROOT/rel) == expected, rel
        py_compile.compile(str(stage/rel), doraise=True)
    backup = BASE/'backups'/('cover-cancel-'+time.strftime('%Y%m%d-%H%M%S')+'-'+commit[:12])
    backup.mkdir(parents=True)
    manifest = {'commit': commit, 'files': {}}
    for rel, expected in FILES.items():
        target = backup/rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/rel, target)
        assert sha(target) == expected
        manifest['files'][rel] = {'old': expected, 'new': sha(stage/rel)}
    (backup/'manifest.json').write_text(json.dumps(manifest, indent=2))
    with closing(sqlite3.connect('file:'+str(ROOT/'data/drama_material_jobs.sqlite3')+'?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(str(backup/'before.sqlite3'))) as dst:
            src.backup(dst)
    # Install backwards-compatible callback support first. No live schema,
    # task, reference, approval, notification, or publication data is mutated.
    for rel, expected in FILES.items():
        assert sha(ROOT/rel) == expected, rel
        install(stage/rel, ROOT/rel)
    for rel, entry in manifest['files'].items():
        assert sha(ROOT/rel) == entry['new'], rel
        py_compile.compile(str(ROOT/rel), doraise=True)
    pid = signal_drain()
    print(json.dumps({'backup': str(backup), 'commit': commit, 'draining_pid': pid}))


if __name__ == '__main__':
    main()
