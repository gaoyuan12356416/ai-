"""Install/rollback only the Mobo ranking module and runner; no service restart."""
import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import time

from deploy import ROOT, LIVE, STATE, command, sha, read_db, worker_pids, verify_api

BASELINE = {
    'features/screenshot_products.py': 'cc384f59171d26cb90048903d1c3daf2ca4df3732a0bf63611bc7c815c5d30b4',
    'scripts/auto_submit_cover_synthesis.py': '8bfaf8184e336b4258d72485056eea3cfcf07355b89a4eed0826bf2b206e3dee',
}
RECEIPT = STATE / 'top5-deployment.json'


def replace(path, data):
    temporary = path.with_name(path.name + '.selection-new')
    temporary.write_bytes(data)
    os.chmod(str(temporary), path.stat().st_mode & 0o777)
    os.replace(str(temporary), str(path))


def save_json(path, value):
    replace(path, (json.dumps(value, indent=2) + '\n').encode()) if path.exists() else path.write_text(json.dumps(value, indent=2) + '\n')


def restore(receipt):
    state = json.loads((STATE / 'deployment.json').read_text())
    previous = json.loads((Path(receipt['backup']) / 'deployment.json').read_text())
    for relative, item in receipt['files'].items():
        assert sha((LIVE / relative).read_bytes()) in {item['before'], item['after']}, 'live drift: ' + relative
        assert sha(Path(item['backup']).read_bytes()) == item['before'], 'backup drift'
    for relative, item in receipt['files'].items():
        replace(LIVE / relative, Path(item['backup']).read_bytes())
        old = next(r for r in previous['files'] if r['path'] == str(LIVE / relative))
        state['files'] = [old if r['path'] == old['path'] else r for r in state['files']]
    for key in ('profile_commit', 'selection_commit'):
        if key in previous:
            state[key] = previous[key]
        else:
            state.pop(key, None)
    save_json(STATE / 'deployment.json', state)
    receipt['status'] = 'rolled_back'
    save_json(RECEIPT, receipt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    assert command('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 5 * 1024 ** 3
    with open(str(LIVE / 'logs/auto_cover_synthesis.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            restore(json.loads(RECEIPT.read_text()))
            print('ROLLED_BACK: previous selection restored; tasks/images retained')
            return
        assert not RECEIPT.exists(), 'deployment already attempted; reconcile receipt before another attempt'
        assert not command('git', '-C', str(ROOT), 'status', '--porcelain')
        commit = command('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
        assert commit == command('git', '-C', str(ROOT), 'rev-parse', 'origin/codex/screenshot-mobo-products-20261019')
        for relative, expected in BASELINE.items():
            assert sha((LIVE / relative).read_bytes()) == expected, 'live drift: ' + relative
            compile((ROOT / relative).read_bytes(), relative, 'exec')
        state = json.loads((STATE / 'deployment.json').read_text())
        assert state['cron_products_activated'] and state['upload_acceptance'] == 'verified'
        pids = worker_pids()
        api_pid = command('systemctl', 'show', '-p', 'MainPID', '--value', 'drama-material-api')
        backup = STATE / ('before-mobo-top5-' + time.strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(mode=0o700)
        shutil.copy2(str(STATE / 'deployment.json'), str(backup / 'deployment.json'))
        cron = command('crontab', '-l')
        (backup / 'crontab.txt').write_text(cron + '\n')
        with closing(read_db()) as src, closing(sqlite3.connect(str(backup / 'jobs.sqlite3'))) as dst:
            src.backup(dst)
        with tarfile.open(str(backup / 'frontend.tar.gz'), 'w:gz') as archive:
            archive.add(str(LIVE / 'static'), arcname='service-static')
            for pattern in ('*.html', '*.js', '*.css', 'navigation.json'):
                for path in Path('/usr/share/nginx/html').glob(pattern):
                    archive.add(str(path), arcname='nginx/' + path.name)
        receipt = {'commit': commit, 'backup': str(backup), 'status': 'prepared', 'files': {},
                   'worker_pids': pids, 'api_pid': api_pid, 'time': time.strftime('%F %T')}
        for relative in BASELINE:
            saved = backup / Path(relative).name
            shutil.copy2(str(LIVE / relative), str(saved))
            receipt['files'][relative] = {'backup': str(saved), 'before': BASELINE[relative],
                                         'after': sha((ROOT / relative).read_bytes())}
        save_json(RECEIPT, receipt)
        try:
            for relative, item in receipt['files'].items():
                replace(LIVE / relative, (ROOT / relative).read_bytes())
                assert sha((LIVE / relative).read_bytes()) == item['after']
                record = next(r for r in state['files'] if r['path'] == str(LIVE / relative))
                record.update(expected_sha256=item['after'], after_sha256=item['after'])
            state.update(profile_commit=commit, selection_commit=commit)
            save_json(STATE / 'deployment.json', state)
            verify_api()
            assert worker_pids() == pids
            assert command('systemctl', 'show', '-p', 'MainPID', '--value', 'drama-material-api') == api_pid
            assert command('crontab', '-l') == cron
            receipt['status'] = 'deployed'
            save_json(RECEIPT, receipt)
        except Exception:
            restore(receipt)
            raise
        print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
