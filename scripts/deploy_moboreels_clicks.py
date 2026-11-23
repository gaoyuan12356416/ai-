"""Stage a GitHub checkout over the active Moboreels release and switch only its sidecar."""
import argparse
from contextlib import contextmanager, closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
from urllib.request import urlopen

from moboreels_click_history import capture


SOURCE = Path(__file__).resolve().parents[1]
CURRENT = Path('/opt/moboreels-library/current')
MAIN = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
DATA = Path('/mnt/data-disk/moboreels-library/data')
UNIT = 'moboreels-library.service'
CODE = ['features/moboreels_library/'+name+'.py' for name in ('service','short_links','clicks')]
STATIC = ['static/moboreels-report.'+ext for ext in ('js','css','html')]
PROFILES = {
    'order-events': ['features/moboreels_library/orders.py'],
    'report-speed': ['features/moboreels_library/' + name + '.py' for name in ('service', 'orders', 'effects')]
                    + ['static/moboreels-report.html', 'static/moboreels-effects.js'],
    'effects': ['features/moboreels_library/' + name + '.py' for name in ('service', 'orders', 'effects')]
               + ['static/moboreels-report.html', 'static/moboreels-report.css', 'static/moboreels-effects.js'],
    'clicks': CODE + STATIC,
    'library': ['features/moboreels_library/service.py', 'features/moboreels_library/short_links.py']
               + ['static/moboreels-library.' + ext for ext in ('js', 'css', 'html')],
}


def run(*args, **kw):
    return subprocess.check_output(args, text=True, **kw).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def install(path, source):
    if source is None:
        path.unlink()
        return
    tmp = path.with_name(path.name+'.click-'+str(os.getpid()))
    shutil.copy2(source, tmp)
    if path.exists():
        s = path.stat()
        os.chown(tmp, s.st_uid, s.st_gid)
        os.chmod(tmp, s.st_mode & 0o7777)
    else:
        tmp.chmod(0o644)
    tmp.replace(path)


def stage(folder, profile='clicks', baseline='HEAD^', mirror_baselines=None):
    previous = CURRENT.resolve()
    candidate = folder/'candidate'
    if run('git','-C',SOURCE,'status','--porcelain','--untracked-files=no'):
        raise RuntimeError('dirty source checkout')
    commit = run('git','-C',SOURCE,'rev-parse','HEAD')
    folder.mkdir(parents=True, exist_ok=False)
    (folder/'backups').mkdir()
    run('cp','-a','--reflink=auto',previous,candidate)
    manifest = {'commit':commit, 'previous':str(previous), 'profile':profile, 'files':{}, 'overlays':{}}
    for name in PROFILES[profile]:
        result = subprocess.run(['git','-C',SOURCE,'show',baseline+':'+name], capture_output=True)
        expected = hashlib.sha256(result.stdout).hexdigest() if not result.returncode else None
        if digest(previous/name) != expected:
            raise RuntimeError('active source differs from Git baseline: '+name)
        install(candidate/name, SOURCE/name)
        manifest['overlays'][name] = {'old':expected, 'new':digest(candidate/name)}
        targets = [MAIN/name] + ([PUBLIC/Path(name).name] if name.startswith('static/') else [])
        for path in targets:
            # An explicitly reviewed mirror-only edit (for example a navigation
            # asset revision) keeps its own before-hash and exact rollback copy.
            mirror_expected = (mirror_baselines or {}).get(str(path), expected)
            if digest(path) != mirror_expected:
                raise RuntimeError('runtime mirror differs from Git baseline: '+str(path))
            backup = folder/'backups'/str(len(manifest['files']))
            if path.exists():
                shutil.copy2(path, backup)
            manifest['files'][str(path)] = {'name':name, 'backup':str(backup), 'old':mirror_expected, 'new':digest(candidate/name)}
    (folder/'manifest.json').write_text(json.dumps(manifest, indent=2))
    rollback = folder/'rollback.sh'
    rollback.write_text('#!/bin/sh\nexec /usr/bin/python3 '+shlex.quote(str(Path(__file__).resolve()))+
                        ' rollback '+shlex.quote(str(folder))+'\n')
    rollback.chmod(0o700)
    print(json.dumps({'staged':commit, 'previous':str(previous)}))


def busy():
    pid = run('systemctl','show',UNIT,'-p','MainPID','--value')
    if run('ss','-Hnt','state','established','( sport = :8864 )'):
        return 'active HTTP request'
    tasks = Path('/proc',pid,'task')
    if pid == '0' or not tasks.exists():
        return ''
    if len(list(tasks.iterdir())) > 2:
        return 'active HTTP request or background worker'
    for name, table, field, states in (
        ('sync.sqlite3','sync_run','payload',('running','queued')),
        ('reports.sqlite3','report_run','payload',('running','queued')),
        ('promotions.sqlite3','promotions','record_json',('creating',)),
        ('media-accounts/registrations.sqlite3','registrations','payload',('submitting',))):
        with closing(sqlite3.connect((DATA/name).as_uri()+'?mode=ro', uri=True)) as conn:
            if any(json.loads(row[0]).get('status') in states for row in conn.execute(f'SELECT {field} FROM {table}')):
                return table
    return ''


@contextmanager
def drained():
    reason = busy()
    if reason:
        raise SystemExit('Moboreels is finishing '+reason+'; retry once complete')
    # Same 8864-only new-connection barrier used by the current production switcher.
    marker = 'mobo-click-switch-'+str(os.getpid())
    rule = ['-p','tcp','--dport','8864','-m','conntrack','--ctstate','NEW','-m','comment',
            '--comment',marker,'-j','REJECT','--reject-with','tcp-reset']
    run('systemd-run','--quiet','--unit='+marker,'--on-active=60s','--',
        '/usr/sbin/iptables','-D','INPUT',*rule)
    try:
        run('iptables','-I','INPUT','1',*rule)
        try:
            reason = busy()
            if reason:
                raise SystemExit('Accepted Moboreels work is finishing: '+reason)
            yield
        finally:
            run('iptables','-D','INPUT',*rule)
    finally:
        run('systemctl','stop',marker+'.timer')


def switch(folder, rollback):
    manifest = json.loads((folder/'manifest.json').read_text())
    old, new = Path(manifest['previous']), folder/'candidate'
    before, after = ('new','old') if rollback else ('old','new')
    expected, target = (new,old) if rollback else (old,new)
    if CURRENT.resolve() != expected:
        raise RuntimeError('another release is active')
    for name, hashes in manifest['overlays'].items():
        if digest(expected/name) != hashes[before] or digest(target/name) != hashes[after]:
            raise RuntimeError('release file changed: '+name)
    for name, row in manifest['files'].items():
        if digest(Path(name)) not in (row[before], row[after]):
            raise RuntimeError('mirror changed: '+name)
    if rollback:
        # Keep unrelated edits made since activation, without modifying the original backup.
        target = Path(tempfile.mkdtemp(prefix='restored-', dir=folder))
        run('cp','-a','--reflink=auto',str(expected)+'/.',target)
        for name in manifest['overlays']:
            install(target/name, old/name if (old/name).exists() else None)
    else:
        # Include concurrent, unrelated edits made to the prior tree since staging.
        run('rsync','-a','--exclude=__pycache__',*['--exclude=/'+n for n in manifest['overlays']],
            str(old)+'/',str(new)+'/')
    with drained():
        if CURRENT.resolve() != expected:
            raise RuntimeError('active release changed during staging')
        prefix = folder/'nginx-prefix.json'
        if not rollback and manifest.get('profile', 'clicks') == 'clicks' and not prefix.exists():
            with prefix.open('x') as out:
                json.dump(capture(Path('/var/log/nginx')), out)
        link = CURRENT.with_name('current.click-'+str(os.getpid()))
        link.symlink_to(target)
        link.replace(CURRENT)
        run('systemctl','restart',UNIT)
    for _ in range(60):
        try:
            with urlopen('http://127.0.0.1:8864/health', timeout=2) as reply:
                if json.load(reply).get('ok'):
                    break
        except OSError:
            pass
        time.sleep(.2)
    else:
        raise RuntimeError('Moboreels health check failed')
    for name, row in manifest['files'].items():
        path = Path(name)
        if digest(path) == row[after]:
            continue
        if digest(path) != row[before]:
            raise RuntimeError('mirror changed during switch: '+name)
        src = (Path(row['backup']) if row['old'] else None) if rollback else new/row['name']
        install(path, src)
        if digest(path) != row[after]:
            raise RuntimeError('mirror verification failed: '+name)
    print(json.dumps({'status':'rolled_back' if rollback else 'active', 'release':str(target)}))


if __name__ == '__main__':
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('action', choices=('stage','apply','rollback'))
    args.add_argument('folder', type=Path)
    args.add_argument('--profile', choices=PROFILES, default='clicks')
    args.add_argument('--baseline', default='HEAD^', help='Verified source commit for the currently active files')
    args.add_argument('--mirror-baselines', type=Path, help='Reviewed JSON mapping of mirror paths to their existing SHA-256 hashes')
    opts = args.parse_args()
    folder = opts.folder.resolve()
    if (not folder.is_relative_to('/mnt/data-disk') or
            run('findmnt','-n','-o','UUID','--mountpoint','/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'):
        raise SystemExit('Moboreels data disk unavailable')
    if opts.action == 'stage':
        stage(folder, opts.profile, opts.baseline,
              json.loads(opts.mirror_baselines.read_text()) if opts.mirror_baselines else None)
    else:
        with (folder/'switch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            switch(folder, opts.action == 'rollback')
