"""Deploy the regular-video feature from verified Git checkouts. Rollback is code-only."""
from __future__ import annotations

import argparse
import ast
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time


CPU_DATA = Path('/mnt/data-disk')
GPU_DATA = Path('/data/ai-video-editing')
HOST = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
EDITOR = Path('/opt/ai-video-editing')
NGINX = Path('/etc/nginx/default.d/ai-video-editing.conf')
TITLE_NGINX = Path('/etc/nginx/default.d/youtube-regular-titles.conf')
TITLE_CONF = 'ops/youtube-regular/title-route.conf'
PAGES = {'video-editing-ai.html','youtube-publish.html'}
NAV_URL = re.compile(rb'(?<=src=")/quick-nav\.js(?:\?v=[a-zA-Z0-9._-]+)?(?=")')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
BASE = {'editor': 'fa0a7eabc7ceae0fa70c32b1c2bc79db66428eb7',
        'publisher': '0651959c9b77f5af234387fe861a157c564f4c4b'}
API_UNIT = 'drama-material-api.service'
PREPARATION = 'youtube-auto-publish-worker.service'
RENDER = 'ai-video-editing-render.service'
EDITOR_API = 'ai-video-editing.service'


def cmd(*args, **opts):
    return subprocess.check_output(list(map(str, args)), **opts)


def git(repo, *args):
    return cmd('git', '-C', repo, *args)


def blob(repo, ref, name):
    value = subprocess.run(['git', '-C',str(repo),'show', f'{ref}:{name}'], capture_output=True)
    return value.stdout if value.returncode == 0 else None


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def filebytes(path):
    return path.read_bytes() if path.is_file() else None


def atomic(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat() if path.exists() else None
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode if mode is not None else (previous.st_mode & 0o7777 if previous else 0o644))
            if previous:
                os.fchown(handle.fileno(), previous.st_uid, previous.st_gid)
            os.fsync(handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def save(folder, record):
    atomic(folder/'manifest.json',(json.dumps(record, ensure_ascii=False, indent=2)+'\n').encode(),0o600)


def link(path, target):
    temp = path.with_name('.regular-next-'+str(os.getpid()))
    temp.symlink_to(target, target_is_directory=True)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def snapshot(db, folder):
    if not db.is_file():
        raise ValueError('Database missing: '+str(db))
    destination = folder / ('before-'+db.name)
    with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as src:
        with closing(sqlite3.connect(str(destination))) as dst:
            src.backup(dst, pages=1024, sleep=.05)
    os.chmod(destination,0o600)


def service(unit):
    value = cmd('systemctl','show',unit,'-p','ActiveState','-p','SubState','-p','MainPID').decode()
    return dict(line.split('=',1) for line in value.splitlines())


def restart_main_only(unit):
    # Both workers explicitly finish their current item on SIGTERM. Sending it
    # just to their main PID keeps ffmpeg/model subprocesses and their receipts alive.
    if cmd('systemctl','show',unit,'-p','Restart','--value').strip()!=b'always':
        raise ValueError('Cannot rely on automatic worker restart: '+unit)
    cmd('systemctl','kill','--kill-who=main','--signal=TERM',unit)


def await_restarts(pids):
    deadline=time.monotonic()+7200
    while time.monotonic()<deadline:
        if all((s:=service(u))['ActiveState']=='active' and s['MainPID'] not in ('0',pid)
               for u,pid in pids.items()):
            return
        time.sleep(2)
    raise RuntimeError('Service is still draining. Inspect the original processes and manifest.')


def check_editor_work():
    db=CPU_DATA/'ai-video-editing/editor.sqlite3'
    with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=4)) as con:
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks={'studio_runs':"status IN ('analyzing','preparing') AND expires>strftime('%s','now')",
                'catalog_imports':"status='downloading' AND expires>strftime('%s','now')",
                'ai_edit_smart_analysis':"status='running' AND expires>strftime('%s','now')",
                'ai_youtube_final_material':"lease<>'' AND expires>strftime('%s','now')"}
        for name,expr in checks.items():
            if name in tables and con.execute('SELECT 1 FROM '+name+' WHERE '+expr+' LIMIT 1').fetchone():
                raise RuntimeError('Editor CPU work is active: '+name+'; retry after that work finishes')


def check_rollback(scope):
    if scope=='editor-gpu':
        return  # Roll back CPU admission before the renderer; see README.
    db=(CPU_DATA/'ai-video-editing/editor.sqlite3' if scope=='editor-cpu'
        else HOST/'data/drama_material_jobs.sqlite3')
    with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as con:
        if scope=='publisher':
            pending=con.execute("""SELECT 1 FROM youtube_auto_preparation p
                WHERE json_extract(p.body,'$.regular_media.sha256') IS NOT NULL
                AND p.state NOT IN ('cancelled','submitted') AND NOT EXISTS
                (SELECT 1 FROM drama_youtube_publish l WHERE l.preparation_id=p.id) LIMIT 1""").fetchone()
        else:
            pending=con.execute("""SELECT 1 FROM tasks WHERE status NOT IN ('completed','failed')
                AND json_extract(unit_data,'$.youtube_regular') IS NOT NULL LIMIT 1""").fetchone()
            pending=pending or con.execute("""SELECT 1 FROM studio_runs WHERE status NOT IN ('completed','failed')
                AND json_extract(snapshot,'$.config.youtube_regular') IS NOT NULL LIMIT 1""").fetchone()
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='ai_youtube_final_material'").fetchone():
                pending=pending or con.execute("""SELECT 1 FROM ai_youtube_final_material
                    WHERE state IN ('queued','copying','checking','unknown') LIMIT 1""").fetchone()
        if pending:
            raise RuntimeError('This version still owns unfinished regular-video work. Finish or reconcile those items before code rollback.')


def app_splice(live, old, new):
    start=b'        if path == "/short-links" or path.startswith("/short-links/"):'
    end=b'        if "/x-share" in path:'
    def block(data):
        pos=data.index(start)
        return data[pos:data.index(end,pos)]
    before, after = block(old), block(new)
    if live.count(before)!=1 or block(live)!=before:
        raise RuntimeError('YouTube app route changed independently')
    return live.replace(before,after,1)


def page_splice(live, old, new):
    # Navigation deployments maintain their own cache key on every backend page.
    if any(data is None for data in (live,old,new)):
        raise RuntimeError('Expected the existing page and its navigation script')
    source, current = NAV_URL.findall(old), NAV_URL.findall(live)
    if len(source)!=1 or len(current)!=1 or NAV_URL.findall(new)!=source or NAV_URL.sub(source[0],live)!=old:
        raise RuntimeError('Page changed beyond its shared navigation script')
    return NAV_URL.sub(current[0],new)


def names(repo, base, version, scope):
    changed=git(repo,'diff','--no-renames','--name-only',base,version).decode().splitlines()
    if scope=='publisher':
        return [p for p in changed if p in ('app.py','features/drama_synthesis/core.py', TITLE_CONF)
                or p.startswith(('features/youtube_auto_publish/regular','features/youtube_auto_publish/service.py',
                                 'static/youtube-regular','static/youtube-publish'))]
    return [p for p in changed if p.startswith(('features/ai_video_editing/', 'static/video-editing-ai'))
            or p=='deploy/ai-video-editing-nginx.conf']


def add_file(plan, target, live, new):
    if live!=new:
        plan[target]=(live,new)


def build(args, folder):
    repo, scope, version=args.repo, args.scope, args.version
    track='publisher' if scope=='publisher' else 'editor'
    base=BASE[track]
    if git(repo,'rev-parse','HEAD').decode().strip()!=version or git(repo,'diff','HEAD','--name-only').strip():
        raise RuntimeError('Source checkout differs from requested commit')
    changed=names(repo,base,version,scope)
    if not changed:
        raise RuntimeError('No scoped changes in source checkout')
    plan, package = {}, {}
    prev=release=current=None
    if scope!='publisher':
        home=GPU_DATA if scope=='editor-gpu' else EDITOR
        current=home/'current'
        prev=current.resolve(strict=True)
        release=home/'releases'/('youtube-regular-'+version)
    for name in changed:
        old,new=blob(repo,base,name),blob(repo,version,name)
        if new is None: raise ValueError('Unsupported removed runtime file: '+name)
        if name.endswith('.py'): compile(new,name,'exec')
        if name.startswith('features/ai_video_editing/'):
            target=Path(name if scope=='editor-gpu' else name.removeprefix('features/'))
            live=filebytes(prev/target)
            # Older Hong Kong packages were checked out on Windows before installation.
            if live is not None and scope=='editor-gpu' and name.endswith('.py'):
                live=live.replace(b'\r\n',b'\n')
            if live!=old: raise RuntimeError('Editor baseline changed: '+name)
            package[target]=new
            continue
        if scope=='editor-gpu': continue
        if name==TITLE_CONF:
            targets=[TITLE_NGINX]
        elif name.startswith('static/'):
            targets=[HOST/name,WEB/Path(name).name]
        elif name.startswith('deploy/'):
            targets=[NGINX]
        else:
            targets=[HOST/name]
        for target in targets:
            live=filebytes(target)
            if name=='app.py': result=app_splice(live,old,new)
            elif target.name in PAGES: result=page_splice(live,old,new)
            elif live!=old: raise RuntimeError('Scoped runtime file differs from baseline: '+str(target))
            else: result=new
            if name.endswith('.py'): ast.parse(result,filename=str(target))
            add_file(plan,target,live,result)
    if scope.startswith('editor'):
        if release.exists(): raise RuntimeError('Check existing editor release before switching: '+str(release))
        shutil.copytree(prev,release,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for target,data in package.items():
            atomic(release/target,data)
    record={'scope':scope,'version':version,'from_version':args.from_version,'repo':str(repo),'state':'prepared','files':[],
            'previous':str(prev) if prev else None,'release':str(release) if release else None,
            'current':str(current) if current else None,'time':datetime.now(timezone.utc).isoformat()}
    for i,(target,(before,after)) in enumerate(plan.items()):
        if before is not None:
            atomic(folder/str(i),before,0o600)
        record['files'].append({'target':str(target),'old':digest(before),'new':digest(after),
                                'backup':str(i) if before is not None else None})
    save(folder,record)
    return record,plan


def reassemble(args,record,folder):
    """Resume an interrupted install without overwriting the original backups."""
    plan={}
    for row in record['files']:
        target=Path(row['target'])
        live=filebytes(target)
        if digest(live)==row['new']:continue
        if digest(live)!=row['old']:raise RuntimeError('Concurrent file change: '+str(target))
        if target==HOST/'app.py':
            new=app_splice(live,blob(args.repo,BASE['publisher'],'app.py'),blob(args.repo,args.version,'app.py'))
        else:
            if target==NGINX: name='deploy/ai-video-editing-nginx.conf'
            elif target==TITLE_NGINX: name=TITLE_CONF
            elif target.parent==WEB: name='static/'+target.name
            else: name=str(target.relative_to(HOST))
            new=blob(args.repo,args.version,name)
            if target.name in PAGES:
                track='publisher' if args.scope=='publisher' else 'editor'
                new=page_splice(live,blob(args.repo,BASE[track],name),new)
        if digest(new)!=row['new']:raise RuntimeError('Release contents changed: '+str(target))
        if row['backup'] is not None and digest(filebytes(folder/row['backup']))!=row['old']:
            raise RuntimeError('Backup differs: '+str(target))
        plan[target]=(live,new)
    return plan


def update_publisher_ui(args,folder):
    repo, version = args.repo, args.version
    if git(repo,'rev-parse','HEAD').decode().strip()!=version:
        raise RuntimeError('Source checkout differs from requested commit')
    parent=git(repo,'rev-parse',version+'^').decode().strip()
    changed=names(repo,parent,version,'publisher')
    if not changed or any(not name.startswith('static/youtube-publish') for name in changed):
        raise RuntimeError('Expected a static-only publisher update')
    installing=args.action=='install'
    manifest=folder/'manifest.json'
    if not manifest.exists() and not installing:
        raise RuntimeError('Missing UI installation manifest')
    record=json.loads(manifest.read_text()) if manifest.exists() else {
        'scope':args.scope,'version':version,'parent':parent,'state':'prepared','files':[],
        'time':datetime.now(timezone.utc).isoformat()}
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    plan={}
    first=not manifest.exists()
    for name in changed:
        old,new=blob(repo,parent,name),blob(repo,version,name)
        if old is None or new is None:raise RuntimeError('Expected existing UI files')
        before,after=(old,new) if installing else (new,old)
        for path in (HOST/name, WEB/Path(name).name):
            live=filebytes(path)
            is_page=path.name in PAGES
            if live is None:raise RuntimeError('Missing UI file: '+str(path))
            already_set=live==after
            if is_page:
                already_set=NAV_URL.sub(b'/quick-nav.js',live)==NAV_URL.sub(b'/quick-nav.js',after)
            if not first and already_set:
                continue
            if is_page:result=page_splice(live,before,after)
            elif live==before:result=after
            else:raise RuntimeError('UI file changed independently: '+str(path))
            if first:
                backup=str(len(record['files']))
                atomic(folder/backup,live,0o600)
                record['files'].append({'target':str(path),'backup':backup,'old':digest(live),'new':digest(result)})
            plan[path]=(live,result)
    save(folder,record)
    for path,(before,after) in plan.items():
        if filebytes(path)!=before:raise RuntimeError('Concurrent UI change: '+str(path))
        atomic(path,after)
    record['state']='active' if installing else 'rolled_back'
    save(folder,record)
    print(json.dumps({'status':record['state'],'version':version,'backup':str(folder),'files':len(record['files'])}))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('install','rollback'))
    p.add_argument('scope',choices=('editor-gpu','editor-cpu','publisher','publisher-ui'))
    p.add_argument('repo',type=Path)
    p.add_argument('version')
    p.add_argument('--from-version',help='Previously deployed ordinary-video publisher commit for an incremental update')
    args=p.parse_args()
    scope=args.scope
    if scope!='editor-gpu' and cmd('findmnt','-no','UUID','--target',CPU_DATA).decode().strip()!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Wrong CPU data mount')
    folder=(GPU_DATA if scope=='editor-gpu' else CPU_DATA)/'youtube-regular-deploy'/'backups'/scope/args.version
    if scope=='publisher-ui':
        return update_publisher_ui(args,folder)
    if (folder/'manifest.json').is_file():
        prior=json.loads((folder/'manifest.json').read_text()).get('from_version')
        if args.from_version and prior!=args.from_version:raise RuntimeError('Incremental baseline differs from original backup')
        args.from_version=prior
    if args.from_version:
        if scope!='publisher' or not re.fullmatch('[0-9a-f]{40}',args.from_version):raise RuntimeError('Invalid incremental publisher baseline')
        git(args.repo,'merge-base','--is-ancestor',args.from_version,args.version)
        git(args.repo,'cat-file','-e',args.from_version+':features/youtube_auto_publish/regular.py')
        BASE['publisher']=args.from_version
    if scope=='editor-cpu':check_editor_work()
    units={'publisher':[PREPARATION,API_UNIT],'editor-gpu':[RENDER],'editor-cpu':[EDITOR_API]}[scope]
    before={u:service(u) for u in units}
    for u,s in before.items():
        if s['ActiveState']!='active' or s['MainPID']=='0':raise RuntimeError('Inspect existing service transition first: '+u)
    installing=args.action=='install'
    if installing:
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(folder,0o700)
        if (folder/'manifest.json').exists():
            record=json.loads((folder/'manifest.json').read_text())
            if record['scope']!=scope or record['version']!=args.version:
                raise RuntimeError('Release record mismatch')
            plan=reassemble(args,record,folder)
            if record['state']=='active' and not plan:
                if record['current'] and Path(record['current']).resolve()!=Path(record['release']):
                    raise RuntimeError('A newer release is active')
                print('Already active:',args.version);return
        else:
            record,plan=build(args,folder)
        if not record.get('database_backed_up'):
            if scope=='editor-cpu': snapshot(CPU_DATA/'ai-video-editing/editor.sqlite3',folder)
            if scope=='publisher': snapshot(HOST/'data/drama_material_jobs.sqlite3',folder)
            record['database_backed_up']=True
            save(folder,record)
    else:
        if not args.from_version: check_rollback(scope)
        record=json.loads((folder/'manifest.json').read_text())
        if record['version']!=args.version or record['scope']!=scope: raise RuntimeError('Rollback record mismatch')
        plan={}
        for row in record['files']:
            target=Path(row['target'])
            live=filebytes(target)
            back=filebytes(folder/row['backup']) if row['backup'] is not None else None
            if digest(back)!=row['old']:raise RuntimeError('Backup checksum mismatch: '+str(target))
            if digest(live)==row['new']:
                new=back
            elif target==HOST/'app.py' and live is not None:
                new=app_splice(live,blob(args.repo,args.version,'app.py'),blob(args.repo,BASE['publisher'],'app.py'))
            elif digest(live)==row['old']:
                continue
            elif target.name in PAGES and live is not None:
                track='publisher' if scope=='publisher' else 'editor'
                rel='static/'+target.name
                new=page_splice(live,blob(args.repo,args.version,rel),blob(args.repo,BASE[track],rel))
            else:
                raise RuntimeError('Review later change before rollback: '+str(target))
            plan[target]=(live,new)
        if record['current'] and Path(record['current']).resolve()!=Path(record['release']):
            raise RuntimeError('A subsequent editor release is active')
    if scope=='publisher':
        mark='install' if installing else 'rollback'
        baseline=folder/('schedulers-before-'+mark+'.json')
        if not baseline.is_file():
            cmd('python3',HEALTH,'snapshot','--lane','youtube','--output',baseline)
        depend='drama-material-job-worker.service'
        dependent_before=service(depend)['ActiveState']
    record['processes_before']=before
    record['state']='installing' if installing else 'rolling_back'
    save(folder,record)
    for target,(old,new) in plan.items():
        if filebytes(target)!=old:raise RuntimeError('Concurrent change: '+str(target))
        if new is None:target.unlink()
        else:atomic(target,new)
    if record['current']:
        link(Path(record['current']),Path(record['release'] if installing else record['previous']))
    nginx_configs=[p for p in (NGINX,TITLE_NGINX) if p in plan]
    if nginx_configs:
        try:
            cmd('nginx','-t')
        except subprocess.CalledProcessError:
            for p in nginx_configs:
                if plan[p][0] is None:p.unlink(missing_ok=True)
                else:atomic(p,plan[p][0])
            raise
        cmd('systemctl','reload','nginx')
    if scope=='editor-gpu':
        restart_main_only(RENDER)
    elif scope=='editor-cpu':
        cmd('systemctl','restart','--no-block',EDITOR_API)
    else:
        restart_main_only(PREPARATION)
        cmd('systemctl','restart','--no-block',API_UNIT)
    await_restarts({u:s['MainPID'] for u,s in before.items()})
    if scope=='publisher':
        cmd('python3',HEALTH,'restore','--baseline',folder/('schedulers-before-'+mark+'.json'),
            '--wait-seconds','25','--output',folder/('schedulers-after-'+mark+'.json'))
        if dependent_before=='active' and service(depend)['ActiveState']!='active':
            cmd('systemctl','start',depend)
    record.update(state='active' if installing else 'rolled_back',processes_after={u:service(u) for u in units})
    save(folder,record)
    print(json.dumps({'status':record['state'],'backup':str(folder),'version':args.version,'units':units}),flush=True)


if __name__=='__main__':
    main()
