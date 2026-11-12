#!/usr/bin/env python3
"""Scoped deployment overlay; business cancellation/migration is a separate CLI."""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

STANDARD = [
    'features/x_posts/drama_profile.py', 'features/x_posts/service.py',
    'features/x_accounts/oauth_service.py', 'scripts/x_post_schedule_runner.py',
    'features/x_tag_revenue/__init__.py', 'features/x_tag_revenue/taxonomy.py',
    'features/x_tag_revenue/tags.py', 'features/x_tag_revenue/hierarchy.py',
    'features/x_tag_revenue/core.py', 'features/x_tag_revenue/tag_dictionary.json',
    'features/x_tag_publish/__init__.py',
    'features/x_tag_publish/transport.py',
]
STATIC = ['quick-nav.js', 'navigation.json', 'x-legacy-publish-redirect.js',
          'x-auto-publish-templates.html', 'x-auto-publish-template.html',
          'x-auto-publish-runs.html', 'x-post-material-pool.html', 'x-post-drama-pool.html']
RETIRED_TRIGGERS = ['x-auto-post-scheduler.timer', 'x-auto-post-runner.timer',
                    'x-auto-post-runner.path', 'x-auto-post-metric.timer']
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
LINK = Path('/opt/x-post-automation/current')
ENV = Path('/etc/x-post-automation.env')

def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)

def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def save(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def mounted(root):
    uuid = subprocess.check_output(['findmnt','-n','-o','UUID','/mnt/data-disk'], text=True).strip()
    if uuid != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_not_mounted')
    if not str(root.resolve()).startswith('/mnt/data-disk/'):
        raise RuntimeError('deployment_path_outside_data_disk')

def active_writes():
    with sqlite3.connect('file:/var/lib/x-post-automation/accounts.sqlite3?mode=ro',uri=True) as c:
        return c.execute("SELECT queue_id,status FROM x_post_publish_log WHERE unknown_outcome=0 AND status IN ('media_uploading','post_creating','repost_creating')").fetchall()

def require_migration():
    receipts = []
    for path, phase in [('/var/lib/x-post-automation/accounts.sqlite3', 'retire'),
                        ('/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3', 'retire'),
                        ('/mnt/data-disk/x-tag-publisher/posts.sqlite3', 'migrate')]:
        with sqlite3.connect('file:'+path+'?mode=ro', uri=True) as c:
            receipts.append({r[0] for r in c.execute('SELECT plan_sha256 FROM x_profile_retirement_audit WHERE phase=?', (phase,))})
    if not set.intersection(*receipts):
        raise RuntimeError('business_retirement_not_completed')

def snapshot_support(root):
    for lane in ('x', 'youtube'):
        path=root/('schedulers-'+lane+'-before.json')
        run('python3',HEALTH,'snapshot','--lane',lane,'--output',str(path),stdout=subprocess.DEVNULL)
        data=json.loads(path.read_text())
        for unit in RETIRED_TRIGGERS:
            data.get('specs',{}).pop(unit,None)
            data.get('states',{}).pop(unit,None)
        save(root/('schedulers-'+lane+'-restore.json'),data)

def restore_support(root, suffix):
    for lane in ('x', 'youtube'):
        run('python3',HEALTH,'restore','--baseline',str(root/('schedulers-'+lane+'-restore.json')),
            '--wait-seconds','25','--output',str(root/('schedulers-'+lane+'-'+suffix+'.json')),stdout=subprocess.DEVNULL)
    run('systemctl','disable','--now',*RETIRED_TRIGGERS,stdout=subprocess.DEVNULL)
    for unit in RETIRED_TRIGGERS:
        state=subprocess.check_output(['systemctl','show',unit,'-p','ActiveState','-p','UnitFileState'],text=True)
        if 'ActiveState=inactive' not in state or 'UnitFileState=disabled' not in state:
            raise RuntimeError('retired_trigger_not_disabled:'+unit)

def stage(source, root, commit):
    mounted(root)
    actual = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    if actual != commit or len(commit) != 40:
        raise RuntimeError('source_commit_mismatch')
    if (root/'manifest.json').exists():
        raise RuntimeError('deployment_already_staged')
    root.mkdir(parents=True,exist_ok=True)
    release = root/'standard'
    previous = LINK.resolve()
    shutil.copytree(previous,release,ignore=shutil.ignore_patterns('__pycache__','.git','*.pyc'))
    hashes = {}
    for rel in STANDARD:
        dest = release/rel;dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source/rel,dest);hashes[rel]=sha(dest)
    run('python3','-m','py_compile',*[str(release/p) for p in STANDARD if p.endswith('.py')])
    targets = []
    for prefix in ['/root/drama_material_service/static','/usr/share/nginx/html']:
        targets += [(str(Path(prefix)/name),'static/'+name) for name in STATIC]
    targets.append(('/root/drama_material_service/features/retired_modules.py','features/retired_modules.py'))
    manifest = dict(commit=commit,source=str(source),previous=str(previous),release=str(release),
                    hashes=hashes,targets=targets,status='staged')
    save(root/'manifest.json',manifest)
    print(json.dumps({'status':'staged','commit':commit,'release':str(release),'files':len(hashes)+len(targets)}))

def profile_token():
    values={}
    for line in Path('/etc/x-tag-publisher.env').read_text().splitlines():
        if '=' not in line or line.lstrip().startswith('#'):continue
        key,value=line.split('=',1)
        if key in ('X_AUTO_POST_INTERNAL_TOKEN','X_TAG_PROFILE_INTERNAL_TOKEN'):
            values[key]=shlex.split(value)[0]
    token=values.get('X_TAG_PROFILE_INTERNAL_TOKEN') or values.get('X_AUTO_POST_INTERNAL_TOKEN')
    if not token or len(token)<32:raise RuntimeError('profile_rpc_credential_missing')
    return token

def wait_health():
    for _ in range(25):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8810/health',timeout=3) as r:
                if r.status==200:return
        except Exception:pass
        time.sleep(1)
    raise RuntimeError('sidecar_health_failed')

def apply(root):
    mounted(root);m=json.loads((root/'manifest.json').read_text())
    if m['status']!='staged' or str(LINK.resolve())!=m['previous']:
        raise RuntimeError('live_release_changed')
    if active_writes():raise RuntimeError('ordinary_publication_still_inflight')
    require_migration()
    for rel,expected in m['hashes'].items():
        if sha(Path(m['release'])/rel)!=expected:raise RuntimeError('staged_file_changed')
    snapshot_support(root)
    backup=root/'backup';backup.mkdir()
    m['backups']=[]
    for i,(dest,rel) in enumerate(m['targets']):
        target=Path(dest);saved=backup/str(i)
        record={'path':dest,'source':rel,'existed':target.exists(),'backup':str(saved)}
        if target.exists():shutil.copy2(target,saved);record['before_sha256']=sha(target)
        m['backups'].append(record)
    shutil.copy2(ENV,backup/'x-post-automation.env');(backup/'x-post-automation.env').chmod(0o600)
    text=ENV.read_text();lines=[line for line in text.splitlines() if not line.startswith('X_DRAMA_PROFILE_INTERNAL_TOKEN=')]
    secret=profile_token()
    lines.append('X_DRAMA_PROFILE_INTERNAL_TOKEN='+shlex.quote(secret))
    ENV.write_text('\n'.join(lines)+'\n');ENV.chmod(0o640)
    m['env_after_sha256']=sha(ENV)
    save(root/'manifest.json',m)
    for record in m['backups']:
        target=Path(record['path']);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(Path(m['source'])/record['source'],target)
        record['after_sha256']=sha(target)
    save(root/'manifest.json',m)
    temporary=LINK.with_name('current.retirement-next')
    if temporary.exists() or temporary.is_symlink():temporary.unlink()
    temporary.symlink_to(m['release']);os.replace(temporary,LINK)
    run('systemctl','disable','--now',*RETIRED_TRIGGERS,stdout=subprocess.DEVNULL)
    # Material and drama share these triggers. Keep them stopped until the
    # old material queues are canceled, then restore normal drama operation.
    run('systemctl','--job-mode=ignore-dependencies','restart','x-post-automation.service')
    wait_health()
    run('systemctl','--job-mode=ignore-dependencies','restart','drama-material-api.service')
    restore_support(root,'after')
    m['status']='active';save(root/'manifest.json',m)
    print(json.dumps({'status':'active','commit':m['commit'],'release':m['release'],'rollback_manifest':str(root/'manifest.json')}))

def rollback(root):
    m=json.loads((root/'manifest.json').read_text())
    if active_writes():raise RuntimeError('ordinary_publication_still_inflight')
    if str(LINK.resolve())!=m['release']:raise RuntimeError('live_release_changed')
    for record in m['backups']:
        target=Path(record['path'])
        if target.exists() and sha(target)!=record['after_sha256']:raise RuntimeError('live_file_changed:'+str(target))
    if sha(ENV)!=m['env_after_sha256']:raise RuntimeError('live_environment_changed')
    for record in m['backups']:
        target=Path(record['path'])
        if record['existed']:shutil.copy2(record['backup'],target)
        else:target.unlink(missing_ok=True)
    shutil.copy2(root/'backup/x-post-automation.env',ENV)
    temporary=LINK.with_name('current.retirement-rollback')
    if temporary.exists() or temporary.is_symlink():temporary.unlink()
    temporary.symlink_to(m['previous']);os.replace(temporary,LINK)
    run('systemctl','--job-mode=ignore-dependencies','restart','x-post-automation.service');wait_health()
    run('systemctl','--job-mode=ignore-dependencies','restart','drama-material-api.service')
    restore_support(root,'rollback')
    m['status']='code_rolled_back';save(root/'manifest.json',m)
    print(json.dumps({'status':'code_rolled_back','business_cancellations_and_migrations_preserved':True}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['stage','apply','rollback']);p.add_argument('--root',required=True);p.add_argument('--source');p.add_argument('--commit');a=p.parse_args()
    if a.mode=='stage':stage(Path(a.source).resolve(),Path(a.root),a.commit)
    elif a.mode=='apply':apply(Path(a.root))
    else:rollback(Path(a.root))
