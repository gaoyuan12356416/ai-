"""Install or revert only the Moboreels catalog timer's calendar override."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE))
from scripts.deploy_pipeline_runtime import verify_source

UNIT = 'moboreels-library-sync.timer'
DROPIN = Path('/etc/systemd/system') / (UNIT + '.d') / '20-hourly.conf'
ARTIFACT = SOURCE / 'deploy' / (UNIT + '.d') / DROPIN.name
CALENDAR = '*-*-* *:00:00 Asia/Shanghai'
DATA = Path('/mnt/data-disk')
UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'


def run(*args):
    return subprocess.check_output(args, text=True, timeout=40).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def status():
    keys = ('ActiveState', 'UnitFileState', 'FragmentPath', 'DropInPaths', 'Triggers',
            'TimersCalendar', 'NextElapseUSecRealtime')
    args = [arg for key in keys for arg in ('-p', key)]
    return dict(line.split('=', 1) for line in run('systemctl', 'show', UNIT, *args).splitlines())


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temp.replace(path)


def prepare(folder):
    commit = verify_source(SOURCE, 'HEAD')
    before = status()
    if 'moboreels-library-sync.service' not in before['Triggers'].split():
        raise RuntimeError('unexpected_sync_timer_target')
    folder.mkdir(mode=0o750, parents=True)
    if DROPIN.exists():
        shutil.copy2(DROPIN, folder / 'override.before')
    shutil.copy2(before['FragmentPath'], folder / 'base-unit.before')
    shutil.copy2(ARTIFACT, folder / 'override.next')
    info = dict(commit=commit, folder=str(folder), before=before, old=digest(DROPIN),
                new=digest(ARTIFACT), status='prepared')
    write_json(folder / 'manifest.json', info)
    return info


def put(file):
    if file is None:
        DROPIN.unlink(missing_ok=True)
    else:
        DROPIN.parent.mkdir(exist_ok=True)
        tmp = DROPIN.with_suffix('.new')
        shutil.copyfile(file, tmp)
        tmp.chmod(0o644)
        tmp.replace(DROPIN)


def reload(active):
    run('systemctl', 'daemon-reload')
    if active == 'active':
        run('systemctl', 'restart', UNIT)


def switch(folder, rollback):
    manifest = folder / 'manifest.json'
    info = json.loads(manifest.read_text())
    old, new = info['old'], info['new']
    pre, post = (new, old) if rollback else (old, new)
    if digest(DROPIN) not in (pre, post):
        raise RuntimeError('catalog_timer_override_changed_since_prepare')
    state = status()
    if state['ActiveState'] not in ('active', 'inactive'):
        raise RuntimeError('catalog_timer_transition_in_progress')
    oldfile = folder / 'override.before' if old else None
    newfile = folder / 'override.next'
    if digest(newfile) != new or (old and digest(oldfile) != old):
        raise RuntimeError('catalog_timer_release_file_changed')
    target, previous = (oldfile, newfile) if rollback else (newfile, oldfile)
    try:
        put(target)
        reload(state['ActiveState'])
        after = status()
        if (after['ActiveState'], after['UnitFileState']) != (state['ActiveState'], state['UnitFileState']):
            raise RuntimeError('catalog_timer_activation_state_changed')
        if not rollback and CALENDAR not in after['TimersCalendar']:
            raise RuntimeError('hourly_schedule_not_loaded')
        if after['ActiveState'] == 'active' and after['NextElapseUSecRealtime'] in ('', 'n/a'):
            raise RuntimeError('catalog_timer_next_trigger_missing')
    except Exception:
        put(previous)
        reload(state['ActiveState'])
        raise
    info.update(status='rolled_back' if rollback else 'active', observed=after)
    write_json(manifest, info)
    return info


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if DATA not in folder.parents or run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', str(DATA)) != UUID:
        raise RuntimeError('expected_data_disk_required')
    result = prepare(folder) if args.action == 'prepare' else switch(folder, args.action == 'rollback')
    print(json.dumps(result, ensure_ascii=False))
