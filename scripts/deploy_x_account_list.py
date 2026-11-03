"""GitHub-first account-list overlay with X-only drain and code-only rollback."""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path('/mnt/data-disk/x-post-automation')
CURRENT = Path('/opt/x-post-automation/current')
MAIN = Path('/root/drama_material_service')
DB = Path('/var/lib/x-post-automation/accounts.sqlite3')
TOKENS = Path('/var/lib/x-post-automation/tokens')
MODULE = 'features/x_accounts/oauth_service.py'
CLIENT = 'features/x_accounts/client.py'
CREDITS = 'features/x_accounts/credits.py'
PAGE = 'static/x-account-list.html'
BASELINE = {MODULE: 'c10b3898ffa48ce79285b37bfc8493dd214fa58e8c35d13c2be96f6568709056',
            CLIENT: 'd18c29cbabd0f20b3a23726bfc3ecfdb3dadfc84cab6a5a56b87eb3e1fc663b5',
            'app.py': '7f863ca1094dac54352485484dc40feda280adf579a77dbcfd8bae0d810ec6c7',
            PAGE: '671eb7d890c784c65deb802e85edd4f649da2db9389207a9ea2dde9bee0737f1'}
TRIGGERS = ['x-post-schedule.timer','x-post-schedule-claim.timer','x-post-manual.timer',
            'x-post-manual.path','x-post-daily.timer','x-auto-post-runner.timer',
            'x-auto-post-scheduler.timer','x-auto-post-runner.path','x-auto-post-metric.timer',
            'x-api-credit-monitor.timer','x-video-pool-scheduler.timer']
WORKERS = [u.replace('.timer','.service') for u in TRIGGERS if u.endswith('.timer')]
UNITS = ['x-post-automation.service','x-auto-post-service.service','drama-material-api.service']


def run(*args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if p.returncode: raise RuntimeError('command failed: ' + ' '.join(args[:3]))
    return p.stdout.strip()


def state(unit): return run('systemctl','show',unit,'-p','ActiveState','--value')
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def tree(root):
    return {p.relative_to(root).as_posix(): ('link:'+os.readlink(p) if p.is_symlink() else digest(p))
            for p in root.rglob('*') if (p.is_file() or p.is_symlink())
            and '__pycache__' not in p.parts and '.git' not in p.parts and p.suffix!='.pyc'}
def save(folder, manifest):
    temp = folder / 'manifest.next'
    temp.write_text(json.dumps(manifest, indent=2) + '\n');temp.chmod(0o600)
    temp.replace(folder / 'manifest.json')


def copy(source, target):
    old = target.stat() if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + '.account-list-next')
    shutil.copy2(source, tmp)
    if old: os.chown(tmp, old.st_uid, old.st_gid);tmp.chmod(old.st_mode & 0o7777)
    else: tmp.chmod(0o644)
    tmp.replace(target)


def switch(release):
    tmp = CURRENT.with_name('account-list-next')
    tmp.symlink_to(release);tmp.replace(CURRENT)


def readonly(path):
    c = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    c.execute('PRAGMA query_only=ON');return c


def pipeline():
    sys.path.insert(0, '/opt/pipeline-dispatch/current')
    from features.pipeline_dispatch.core import DispatchStore
    return DispatchStore('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')


def counts():
    with contextlib.closing(readonly('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')) as c:
        jobs = c.execute("SELECT COUNT(*) FROM pipeline_job WHERE channel='x' AND status='running'").fetchone()[0]
    with contextlib.closing(readonly(DB)) as c:
        publishing = c.execute("SELECT COUNT(*) FROM x_post_queue WHERE status='publishing'").fetchone()[0]
    with contextlib.closing(readonly(ROOT/'youtube-shares/ledger.sqlite3')) as c:
        shares = c.execute("SELECT COUNT(*) FROM youtube_share_item WHERE status IN ('queued','publishing')").fetchone()[0]
    callers = [u for u in WORKERS if state(u) in {'active','activating','deactivating'}]
    accepted = sum(bool(run('ss','-Htn','state','established','sport','=',':'+str(p))) for p in (8810,18833))
    return {'pipeline_jobs':jobs,'publishing_queues':publishing,'shares':shares,'callers':callers,'accepted_ports':accepted}


def ready(port):
    path = '/api/ui/topbar' if port == 8787 else '/health'
    deadline = time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%s%s'%(port,path),timeout=3) as r:
                if r.status==200:return
        except Exception: pass
        time.sleep(1)
    raise RuntimeError('service not ready: '+str(port))


def prepare():
    assert run('findmnt','-no','UUID','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage(ROOT).free>2*1024**3
    assert not run('git','-C',str(SOURCE),'status','--porcelain')
    commit=run('git','-C',str(SOURCE),'rev-parse','HEAD')
    assert run('git','-C',str(SOURCE),'branch','-r','--contains',commit)
    for relative,expected in BASELINE.items():
        live=(CURRENT if relative==MODULE else MAIN)/relative
        assert digest(live)==expected,'baseline changed: '+relative
    assert digest(Path('/usr/share/nginx/html/x-account-list.html'))==BASELINE[PAGE]
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-account-list-'+commit[:8]
    folder=ROOT/'backups'/name;folder.mkdir(mode=0o700)
    release=ROOT/'releases'/name
    shutil.copytree(CURRENT.resolve(),release,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for relative in (MODULE,CLIENT,CREDITS,PAGE):copy(SOURCE/relative,release/relative)
    # Validate the real composite without importing production token code.
    for relative in (MODULE,CLIENT,CREDITS):compile((release/relative).read_bytes(),relative,'exec')
    external={str(MAIN/'app.py'):'app.py',str(MAIN/CLIENT):CLIENT,str(MAIN/CREDITS):CREDITS,
              str(MAIN/PAGE):PAGE,'/usr/share/nginx/html/x-account-list.html':PAGE}
    backups={}
    for index,(target,relative) in enumerate(external.items()):
        path=Path(target);name='external-%d'%index
        if path.exists():shutil.copy2(path,folder/name)
        backups[target]={'relative':relative,'backup':name if path.exists() else '',
                         'before':digest(path) if path.exists() else None,'after':digest(SOURCE/relative)}
    manifest={'commit':commit,'previous_release':str(CURRENT.resolve()),'new_release':str(release),
              'external':backups,'status':'prepared','folder':str(folder),
              'previous_tree':tree(CURRENT.resolve()),'new_tree':tree(release)}
    save(folder,manifest)
    print(json.dumps({'status':'prepared','folder':str(folder),'release':str(release),'commit':commit}),flush=True)


def apply(folder, rollback=False, timeout=900):
    manifest=json.loads((folder/'manifest.json').read_text())
    assert manifest['status'] in ({'complete'} if rollback else {'prepared'})
    expected=manifest['new_release'] if rollback else manifest['previous_release']
    assert str(CURRENT.resolve())==expected,'current runtime changed'
    for target,item in manifest['external'].items():
        actual=digest(target) if Path(target).exists() else None
        assert actual==item['after' if rollback else 'before'],'external runtime changed: '+target
    controller=pipeline();config=controller.config()
    enabled='x' in config['enabled_channels'];prepare_only='x' in config.get('preparation_only_channels',[])
    triggers={u:state(u) for u in TRIGGERS}
    services={u:state(u) for u in UNITS}
    manifest.update(trigger_states=triggers,service_states=services,channel_was_enabled=enabled,
                    channel_was_prepare_only=prepare_only,status='draining')
    save(folder,manifest)
    rule=['OUTPUT','-d','127.0.0.1','-p','tcp','--dport','8810','-m','conntrack','--ctstate','NEW',
          '-m','comment','--comment','x-account-list-'+manifest['commit'][:8],'-j','REJECT','--reject-with','tcp-reset']
    blocked=False;stopped=False;switched=False;external_modified=False
    try:
        if enabled:controller.channel_enabled('x',False)
        for unit,value in triggers.items():
            if value=='active':run('systemctl','stop',unit)
        deadline=time.monotonic()+timeout;last=None
        while True:
            observed=counts()
            if observed!=last:print(json.dumps({'status':'draining',**observed}),flush=True);last=observed
            if not any(observed.values()):break
            if time.monotonic()>deadline:raise RuntimeError('X work still active; restored admissions without code cutover')
            time.sleep(3)
        run('iptables','-w','5','-I',*rule);blocked=True
        # Existing accepted calls may finish; new calls cannot race the stop.
        deadline=time.monotonic()+30
        while run('ss','-Htn','state','established','sport','=',':8810'):
            if time.monotonic()>deadline:raise RuntimeError('accepted sidecar calls still running')
            time.sleep(1)
        assert not counts()['pipeline_jobs'] and not counts()['publishing_queues']
        assert str(CURRENT.resolve())==expected
        assert tree(CURRENT.resolve())==manifest['new_tree' if rollback else 'previous_tree'],'runtime files changed during drain'
        for target,item in manifest['external'].items():
            actual=digest(target) if Path(target).exists() else None
            assert actual==item['after' if rollback else 'before'],'external runtime changed during drain: '+target
        run('systemctl','stop','x-post-automation.service');stopped=True
        shutil.copytree(TOKENS,folder/('tokens-before-rollback' if rollback else 'tokens'))
        with contextlib.closing(readonly(DB)) as source, contextlib.closing(sqlite3.connect(folder/('accounts-before-rollback.sqlite3' if rollback else 'accounts.sqlite3'))) as dst:
            source.backup(dst)
            assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        manifest['token_fingerprints']={p.name:digest(p) for p in TOKENS.glob('*.json')}
        external_modified=True
        for target,item in manifest['external'].items():
            path=Path(target)
            if rollback:
                if item['backup']:copy(folder/item['backup'],path)
                elif path.exists():path.unlink()
            else:copy(SOURCE/item['relative'],path)
        switch(Path(manifest['previous_release'] if rollback else manifest['new_release']));switched=True
        manifest['status']='switched';save(folder,manifest)
        run('systemctl','start','x-post-automation.service')
        run('iptables','-w','5','-D',*rule);blocked=False
        ready(8810)
        if services['x-auto-post-service.service']=='active':run('systemctl','start','x-auto-post-service.service');ready(18833)
        run('systemctl','restart','drama-material-api.service');ready(8787)
        manifest['status']='rolled_back' if rollback else 'complete'
        manifest['token_fingerprints_unchanged']=manifest['token_fingerprints']=={p.name:digest(p) for p in TOKENS.glob('*.json')}
    except BaseException:
        # Failures before cutover leave the previous code; after cutover restore
        # only code, never database or credential snapshots.
        if switched or external_modified:
            run('systemctl','stop','x-post-automation.service')
            if switched:switch(Path(expected))
            for target,item in manifest['external'].items():
                if rollback:copy(SOURCE/item['relative'],Path(target))
                elif item['backup']:copy(folder/item['backup'],Path(target))
                elif Path(target).exists():Path(target).unlink()
            run('systemctl','restart','drama-material-api.service')
        manifest['status']='failed_restored';raise
    finally:
        if blocked:run('iptables','-w','5','-D',*rule)
        if stopped:
            run('systemctl','start','x-post-automation.service');ready(8810)
            if services['x-auto-post-service.service']=='active':run('systemctl','start','x-auto-post-service.service');ready(18833)
        if enabled:controller.channel_enabled('x',True,preparation_only=prepare_only)
        for unit,value in triggers.items():
            if value=='active':run('systemctl','start',unit)
        save(folder,manifest)
    print(json.dumps({'status':manifest['status'],'folder':str(folder),'commit':manifest['commit']}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--prepare',action='store_true');p.add_argument('--apply',type=Path);p.add_argument('--rollback',type=Path)
    a=p.parse_args()
    if a.prepare:prepare()
    elif a.apply:apply(a.apply)
    elif a.rollback:apply(a.rollback,rollback=True)
    else:p.error('select --prepare, --apply or --rollback')
