#!/usr/bin/env python3
"""Stage GitHub code, drain CPU publishers, then cut over GPU and CPU safely."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_x_auto_duration as base
from features.x_posts.media_limits import MAX_POST_VIDEO_BYTES

BASELINE = json.loads((SOURCE / 'deploy/x-media-size-baseline.json').read_text())
GPU_ROOT = Path('/data/x-post-media-repair')
GPU_LINK = GPU_ROOT / 'current'
GPU_PYTHON = GPU_ROOT / 'runtime/python/bin/python'
ENV = {
    'cpu': {
        '/etc/x-post-automation.env': ['X_POST_MAX_MEDIA_BYTES'],
        '/etc/x-post-daily.env': ['X_POST_DAILY_MAX_MEDIA_BYTES'],
        '/etc/x-post-schedule.env': ['X_POST_SCHEDULE_MAX_MEDIA_BYTES'],
        '/etc/x-auto-post.env': ['X_AUTO_POST_MAX_MEDIA_BYTES'],
    },
    'gpu': {'/data/x-post-media-repair/config/worker.env': [
        'X_POST_MEDIA_REPAIR_MAX_SOURCE_BYTES', 'X_POST_MEDIA_REPAIR_MAX_OUTPUT_BYTES']},
}
TESTS = ['scripts.test_x_media_size', 'scripts.test_x_posts', 'scripts.test_x_post_daily',
         'scripts.test_x_post_schedule_runner', 'scripts.test_x_post_media_repair',
         'scripts.test_x_auto_post_service', 'scripts.test_x_random_template',
         'scripts.test_x_post_auto_template_bridge', 'scripts.test_x_post_manual_sidecar',
         'scripts.test_x_post_manual_runner', 'scripts.test_x_post_priority_manual_store',
         'scripts.test_x_post_drama_duration_publish', 'scripts.test_x_post_source_duration']


def save(m):
    path = Path(m['backup']) / 'manifest.json'
    path.write_text(json.dumps(m, indent=2) + '\n')
    path.chmod(0o600)
    print(json.dumps({k:m[k] for k in ['host','status','backup','release','commit']}), flush=True)


def baseline(host):
    roots = {'gpu':GPU_LINK} if host == 'gpu' else {'cpu':base.CURRENT, 'main':base.MAIN}
    for group, root in roots.items():
        for name, expected in BASELINE[group].items():
            actual = base.digest(root/name) if (root/name).exists() else None
            assert actual == expected, 'baseline drift: ' + str(root/name)


def switch(link, target):
    temp = link.with_name(link.name + '.size-next')
    assert not temp.exists() and not temp.is_symlink()
    temp.symlink_to(target)
    temp.replace(link)


def update_env(path, keys):
    content = path.read_text()
    for key in keys:
        line = key + '=' + str(MAX_POST_VIDEO_BYTES)
        pattern = r'(?m)^' + re.escape(key) + r'=.*$'
        if re.search(pattern, content):
            content = re.sub(pattern, line, content)
        else:
            content = content.rstrip('\n') + '\n' + line + '\n'
    path.write_text(content)


def stage(host):
    baseline(host)
    assert not base.run('git','-C',str(SOURCE),'status','--porcelain')
    commit = base.run('git','-C',str(SOURCE),'rev-parse','HEAD')
    root, link = (GPU_ROOT,GPU_LINK) if host == 'gpu' else (base.DATA,base.CURRENT)
    assert shutil.disk_usage(root).free > 3 * MAX_POST_VIDEO_BYTES
    stamp = time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()) + '-media-size-' + commit[:10]
    backup, release = root/'backups'/stamp, root/'releases'/stamp
    backup.mkdir(mode=0o700)
    previous = link.resolve(strict=True)
    shutil.copytree(previous, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in BASELINE[host]:
        base.copy(SOURCE/name, release/name)
    for test in (SOURCE/'scripts').glob('test_x_*.py'):
        base.copy(test, release/'scripts'/test.name)
    # Tests which inspect GPU source use the complete source tree. CPU behavior
    # is checked against the assembled runtime, never a whole-repo replacement.
    modules = ['scripts.test_x_post_media_repair','scripts.test_x_random_template'] if host == 'gpu' else TESTS
    executable = str(GPU_PYTHON) if host == 'gpu' else 'python3'
    with (backup/'regression.txt').open('w') as output:
        subprocess.check_call([executable,'-m','unittest',*modules,'-q'],cwd=release,stdout=output,stderr=output)
    external = list(ENV[host])
    if host == 'cpu':
        external += [str(base.MAIN/name) for name in BASELINE['main']]
    existed = {}
    for target in external:
        existed[target] = Path(target).exists()
        if existed[target]:
            base.copy(Path(target),backup/'files'/target.lstrip('/'))
    m = dict(host=host,commit=commit,backup=str(backup),release=str(release),previous_release=str(previous),
             external_files=existed,external_hashes={target:base.digest(Path(target)) if exists else None
                                                   for target,exists in existed.items()},
             status='prepared',files={name:base.digest(release/name) for name in BASELINE[host]})
    save(m)


def gpu_health(port):
    with urllib.request.urlopen('http://127.0.0.1:%d/health'%port,timeout=3) as response:
        data = json.load(response)
    assert data['status'] == 'ok'
    assert data['max_source_bytes'] == data['max_output_bytes'] == MAX_POST_VIDEO_BYTES
    return data


def restore_files(m):
    for target, existed in m['external_files'].items():
        if existed:
            base.copy(Path(m['backup'])/'files'/target.lstrip('/'),Path(target))
        elif Path(target).exists():
            Path(target).unlink()


def restore_cpu(m):
    base.run('systemctl','stop',*reversed(base.SERVICES))
    restore_files(m)
    switch(base.CURRENT,Path(m['previous_release']))
    for unit in base.SERVICES:
        base.run('systemctl','start',unit)
    base.health()


def gpu_deploy(m, rollback=False):
    assert GPU_LINK.resolve() == Path(m['release'] if rollback else m['previous_release'])
    if not rollback:
        baseline('gpu')
        for target,digest in m['external_hashes'].items():
            assert (base.digest(Path(target)) if Path(target).exists() else None)==digest, 'configuration drift'
        for name,digest in m['files'].items(): assert base.digest(Path(m['release'])/name)==digest
    pid = base.run('systemctl','show','x-post-media-repair.service','-p','MainPID','--value')
    assert len(list(Path('/proc/'+pid+'/task').iterdir())) == 1, 'GPU request in flight'
    try:
        base.run('systemctl','stop','x-post-media-repair.service')
        if rollback:
            restore_files(m)
        else:
            for path,keys in ENV['gpu'].items(): update_env(Path(path),keys)
        switch(GPU_LINK,Path(m['previous_release'] if rollback else m['release']))
        base.run('systemctl','start','x-post-media-repair.service')
        base.run('systemctl','start','x-post-media-repair-tunnel.service')
        deadline = time.monotonic()+30
        while not rollback:
            try:
                m['health'] = gpu_health(8820)
                break
            except (OSError,AssertionError,KeyError):
                if time.monotonic()>deadline: raise
                time.sleep(1)
        m['status'] = 'rolled_back' if rollback else 'deployed'
    except BaseException:
        base.run('systemctl','stop','x-post-media-repair.service')
        restore_files(m)
        switch(GPU_LINK,Path(m['previous_release']))
        base.run('systemctl','start','x-post-media-repair.service')
        base.run('systemctl','start','x-post-media-repair-tunnel.service')
        m['status'] = 'rolled_back'
        raise
    finally:
        save(m)


def cpu_deploy(m, rollback=False):
    expected = Path(m['release'] if rollback else m['previous_release'])
    assert base.CURRENT.resolve() == expected
    with base.paused() as triggers:
        m['triggers'] = triggers
        if rollback:
            restore_cpu(m)
            m['status']='rolled_back'
            save(m)
            return
        baseline('cpu')
        for name,digest in m['files'].items(): assert base.digest(Path(m['release'])/name)==digest
        for target,digest in m['external_hashes'].items():
            assert (base.digest(Path(target)) if Path(target).exists() else None)==digest, 'configuration drift'
        stopped = False
        try:
            base.run('systemctl','stop',*reversed(base.SERVICES))
            stopped = True
            before = {'post':base.db_facts(base.POST_DB),'auto':base.db_facts(base.AUTO_DB)}
            tokens = base.token_facts()
            base.snapshot(base.POST_DB,Path(m['backup'])/'posts.sqlite3')
            base.snapshot(base.AUTO_DB,Path(m['backup'])/'auto.sqlite3')
            m['status']='waiting_for_gpu'
            save(m)
            deadline=time.monotonic()+180
            while True:
                try:
                    m['gpu_health']=gpu_health(18820)
                    break
                except (OSError,AssertionError,KeyError):
                    if time.monotonic()>deadline: raise RuntimeError('GPU compatibility wait expired')
                    time.sleep(1)
            for path,keys in ENV['cpu'].items(): update_env(Path(path),keys)
            for name in BASELINE['main']: base.copy(Path(m['release'])/name,base.MAIN/name)
            switch(base.CURRENT,Path(m['release']))
            for unit in base.SERVICES: base.run('systemctl','start',unit)
            base.health()
            assert {'post':base.db_facts(base.POST_DB),'auto':base.db_facts(base.AUTO_DB)}==before
            assert base.token_facts()==tokens
            for unit,key in [('x-post-automation.service','X_POST_MAX_MEDIA_BYTES'),
                             ('x-auto-post-service.service','X_AUTO_POST_MAX_MEDIA_BYTES')]:
                pid=base.run('systemctl','show',unit,'-p','MainPID','--value')
                env=dict(x.split('=',1) for x in Path('/proc/'+pid+'/environ').read_text().split('\0') if '=' in x)
                assert int(env[key])==MAX_POST_VIDEO_BYTES
            m.update(status='deployed',ledger_unchanged=True,tokens_unchanged=True)
            save(m)
        except BaseException:
            if stopped: restore_cpu(m)
            m['status']='rolled_back'
            save(m)
            raise
    m['triggers_restored']=True
    save(m)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--host',choices=['cpu','gpu'],required=True)
    action=p.add_mutually_exclusive_group(required=True)
    action.add_argument('--stage',action='store_true')
    action.add_argument('--deploy',type=Path)
    action.add_argument('--rollback',type=Path)
    args=p.parse_args()
    if args.host=='cpu':
        assert base.run('findmnt','-no','UUID','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    if args.stage:
        stage(args.host)
    else:
        m=json.loads(((args.deploy or args.rollback)/'manifest.json').read_text())
        assert m['host']==args.host
        (cpu_deploy if args.host=='cpu' else gpu_deploy)(m,bool(args.rollback))


if __name__=='__main__':main()
