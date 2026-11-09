#!/usr/bin/env python3
"""GitHub-first independent X tag lane install and scoped, receipt-preserving rollback."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from features.x_tag_publish.staging import build

ROOT = Path('/mnt/data-disk/x-tag-publisher')
CURRENT = Path('/opt/x-tag-publisher/current')
MAIN = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
ENV = Path('/etc/x-tag-publisher.env')
LEGACY_SOURCE = Path('/mnt/data-disk/x-post-automation/releases/20261109T025536Z-excel-episode-063eefab')
ADAPTER_BASE = 'b511ee11b12476fbe34912534c90c5f62b3bc07397360e0b9b7a97d4f35d3ac5'
ADAPTERS = (MAIN/'features/social_analytics/adapters.py',Path('/opt/social-analytics-warehouse/current/features/social_analytics/adapters.py'))
PARENTS = ('x-tag-post.service','x-tag-auto.service','x-tag-gateway.service')
WORKERS = ('x-tag-auto-scheduler','x-tag-auto-runner','x-tag-pool','x-tag-manual','x-tag-metric')
TRIGGERS = tuple(name+'.timer' for name in WORKERS) + ('x-tag-auto-runner.path',)
ENTRY = '<link rel="stylesheet" href="/x-tag-publish-ui.css?v=1" />\n<script src="/x-tag-publish-entry.js?v=1"></script>\n'
SHORT_INCLUDE = '    include /etc/nginx/snippets/x-tag-post-s2l.conf;\n'
SHORT_CONFIG = '''location ~ ^/s2l-tag/([0-9]+)[.]html$ {
    alias /mnt/data-disk/x-tag-publisher/s2l/$1.html;
    default_type text/html;
    add_header Cache-Control "no-cache" always;
    add_header X-Content-Type-Options nosniff always;
}
'''


def cmd(*args, timeout=120, check=True, cwd=None, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('command_failed:'+args[0]+':'+result.stderr[-700:])
    return result.stdout.strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def atomic(path, data, mode=0o644):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tag-next');tmp.write_bytes(data);tmp.chmod(mode);tmp.replace(path)


def read_env(path):
    values={}
    if Path(path).exists():
        for raw in Path(path).read_text().splitlines():
            if raw and not raw.startswith('#') and '=' in raw:
                key,value=raw.split('=',1);values[key]=value.strip().strip('"').strip("'")
    return values


def environment():
    values={}
    for path in ('/etc/x-post-automation.env','/etc/x-post-daily.env','/etc/x-post-media-repair.token','/etc/x-post-schedule.env','/etc/x-auto-post.env','/etc/x-auto-post.secrets'):
        values.update(read_env(path))
    broker=read_env('/etc/x-post-automation.env')
    values['X_TAG_ACCOUNT_BROKER_TOKEN']=broker.get('X_INTERNAL_TOKEN') or broker.get('X_POST_AUTOMATION_INTERNAL_TOKEN')
    values.update(X_INTERNAL_TOKEN=secrets.token_urlsafe(48),X_POST_DAILY_INTERNAL_TOKEN=secrets.token_urlsafe(48),
                  X_POST_AUTO_INTERNAL_TOKEN=secrets.token_urlsafe(48),X_AUTO_POST_INTERNAL_TOKEN=secrets.token_urlsafe(48))
    values['X_POST_AUTOMATION_INTERNAL_TOKEN']=values['X_INTERNAL_TOKEN']
    values['X_POST_SCHEDULE_INTERNAL_TOKEN']=values['X_POST_DAILY_INTERNAL_TOKEN']
    values.update(X_TAG_STATE_ROOT=str(ROOT),X_POST_DB_PATH=str(ROOT/'posts.sqlite3'),
        X_AUTO_POST_DB_PATH=str(ROOT/'auto/x-auto-post.sqlite3'),X_AUTO_POST_STATE_ROOT=str(ROOT/'auto'),
        X_POST_STORAGE_ROOT=str(ROOT),X_POST_PUBLIC_ROOT=str(ROOT/'s2l'),X_POST_SHORT_BASE_URL='https://gy.g2flow.com/s2l-tag',
        X_POST_STORAGE_MOUNT_ROOT='/mnt/data-disk',X_POST_AUTOMATION_INTERNAL_URL='http://127.0.0.1:8821',
        X_POST_DAILY_INTERNAL_URL='http://127.0.0.1:8821',X_POST_SCHEDULE_INTERNAL_URL='http://127.0.0.1:8821',
        X_AUTO_POST_INTERNAL_URL='http://127.0.0.1:18843',X_AUTO_POST_ADMIN_SERVICE_URL='http://127.0.0.1:18843',X_AUTO_POST_SERVICE_PORT='18843',X_LISTEN_PORT='8821',
        X_ACCOUNTS_DB=broker.get('X_ACCOUNTS_DB','/var/lib/x-post-automation/accounts.sqlite3'),
        X_TOKENS_DIR=broker.get('X_TOKENS_DIR','/var/lib/x-post-automation/tokens'),
        X_POST_DAILY_ACCOUNT_IDS='',X_POST_DAILY_LOCK_PATH='/run/x-tag-post/daily.lock',
        X_POST_SCHEDULE_LOCK_PATH='/run/x-tag-post/schedule.lock',X_POST_MANUAL_LOCK_PATH='/run/x-tag-post/manual.lock',
        X_AUTO_POST_RUNNER_LOCK_PATH='/run/x-tag-auto/runner.lock',X_AUTO_POST_SCHEDULER_LOCK_PATH='/run/x-tag-auto/scheduler.lock',
        X_AUTO_POST_METRIC_LOCK_PATH='/run/x-tag-auto/metric.lock',X_AUTO_POST_RUNNER_KICK_PATH='/run/x-tag-auto/manual-kick',
        X_POST_DAILY_WORK_DIR=str(ROOT/'daily-work'),X_POST_SCHEDULE_WORK_DIR=str(ROOT/'daily-work'),
        X_AUTO_POST_WORK_DIR=str(ROOT/'daily-work'),X_POST_FFPROBE_BIN='/mnt/data-disk/x-post-automation/bin/ffprobe',
        PIPELINE_X_DISPATCH_ENABLED='0',PIPELINE_X_CANONICAL_BRIDGE_ENABLED='0',
        X_POST_ATTRIBUTION_V2_ENABLED='1',X_AUTO_POST_WORKER_COUNT='1',X_POST_SCHEDULE_BATCH_WORKERS='1',
        X_AUTO_POST_METRIC_LOOKBACK_DAYS='7',X_AUTO_POST_RUNNER_ID='x-tag-auto-runner')
    if not values['X_TAG_ACCOUNT_BROKER_TOKEN']:
        raise RuntimeError('account_broker_not_configured')
    return values


def unit(name, user, command, *, after='', oneshot=False, writes=''):
    return f'''[Unit]
Description=Independent X tag profile lane - {name}
After=network-online.target {after}
RequiresMountsFor={ROOT}
ConditionPathIsMountPoint=/mnt/data-disk

[Service]
Type={'oneshot' if oneshot else 'simple'}
User={user}
WorkingDirectory={CURRENT}
EnvironmentFile={ENV}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 {command}
{'Restart=on-failure' if not oneshot else ''}
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome={'false' if user=='root' else 'true'}
ReadWritePaths={writes or str(ROOT)}
ReadOnlyPaths=/var/lib/x-post-automation /mnt/data-disk/x-post-automation
UMask=0077
TimeoutStartSec=360min
TimeoutStopSec=infinity
SendSIGKILL=no

[Install]
WantedBy=multi-user.target
'''


def units():
    result={
      'x-tag-post.service':unit('publisher','x-post-automation','-m features.x_tag_publish.runtime',writes=str(ROOT/'posts')+' '+str(ROOT/'s2l')+' '+str(ROOT)),
      'x-tag-auto.service':unit('automatic templates','x-post-daily',str(CURRENT/'scripts/x_auto_post_service.py'),after='x-tag-post.service',writes=str(ROOT/'auto')+' '+str(ROOT/'daily-work')+' /run/x-tag-auto'),
      'x-tag-gateway.service':unit('browser gateway','root','-m features.x_tag_publish.gateway',after='x-tag-post.service x-tag-auto.service',writes=str(ROOT)),
    }
    scripts={'x-tag-auto-scheduler':('x_auto_post_runner.py --mode tick','/run/x-tag-auto'),
             'x-tag-auto-runner':('x_auto_post_runner.py --mode execute','/run/x-tag-auto'),
             'x-tag-pool':('x_post_schedule_runner.py','/run/x-tag-post'),
             'x-tag-manual':('x_post_manual_runner.py','/run/x-tag-post'),
             'x-tag-metric':('x_auto_post_metric_runner.py','/run/x-tag-auto')}
    for name,(script,run_path) in scripts.items():
        result[name+'.service']=unit(name,'x-post-daily',str(CURRENT/'scripts'/script),
            after='x-tag-post.service x-tag-auto.service',oneshot=True,
            writes=str(ROOT/'auto')+' '+str(ROOT/'daily-work')+' '+run_path)
        calendar='*-*-* *:27:00 Asia/Shanghai' if name=='x-tag-metric' else '*-*-* *:*:20 Asia/Shanghai' if name=='x-tag-pool' else '*-*-* *:*:05 Asia/Shanghai'
        timer='OnBootSec=20s\nOnUnitActiveSec=20s' if name=='x-tag-manual' else 'OnCalendar='+calendar
        result[name+'.timer']=f'[Unit]\nDescription=Independent X tag lane timer\n[Timer]\n{timer}\nPersistent=false\nAccuracySec=1s\nUnit={name}.service\n[Install]\nWantedBy=timers.target\n'
    result['x-tag-auto-runner.path']='[Unit]\nDescription=Wake independent X tag auto runner\n[Path]\nPathChanged=/run/x-tag-auto/manual-kick\nUnit=x-tag-auto-runner.service\n[Install]\nWantedBy=multi-user.target\n'
    return result


def prepare_targets(release):
    targets={str(Path('/etc/systemd/system')/name):value.encode() for name,value in units().items()}
    targets['/etc/tmpfiles.d/x-tag-publisher.conf']=b'd /run/x-tag-post 0700 x-post-daily x-post-daily -\nd /run/x-tag-auto 0700 x-post-daily x-post-daily -\n'
    for p in (release/'static').glob('x-tag-*'):
        for root in (WEB,MAIN/'static'):
            targets[str(root/p.name)]=p.read_bytes()
    for root in (WEB,MAIN/'static'):
        for name in ('x-account-list.html','x-auto-publish-templates.html','x-post-material-pool.html'):
            p=root/name;content=p.read_text(encoding='utf-8')
            if ENTRY not in content:
                if content.count('</body>')!=1:raise RuntimeError('entry_anchor_changed:'+str(p))
                content=content.replace('</body>',ENTRY+'</body>')
            targets[str(p)]=content.encode()
    api=''
    for prefix in ('/api/admin/x-tag-auto-publish','/api/admin/x-tag-posts'):
        api+='location ^~ '+prefix+' {\n proxy_pass http://127.0.0.1:8898;\n proxy_set_header Host $host;\n proxy_set_header X-Forwarded-Proto $scheme;\n proxy_read_timeout 150s;\n client_max_body_size 2m;\n}\n'
    targets['/etc/nginx/default.d/x-tag-publisher.conf']=api.encode()
    targets['/etc/nginx/snippets/x-tag-post-s2l.conf']=SHORT_CONFIG.encode()
    path=Path('/etc/nginx/conf.d/gy.g2flow.com.conf');content=path.read_text()
    if SHORT_INCLUDE not in content:
        anchor='    include /etc/nginx/snippets/fb-video-publish-s2l.conf;\n'
        if content.count(anchor)!=1:raise RuntimeError('short_domain_anchor_changed')
        content=content.replace(anchor,anchor+SHORT_INCLUDE)
    targets[str(path)]=content.encode()
    for path in ADAPTERS:
        if sha(path) not in {ADAPTER_BASE,sha(SOURCE/'features/social_analytics/adapters.py')}:
            raise RuntimeError('analytics_adapter_changed:'+str(path))
        targets[str(path)]=(SOURCE/'features/social_analytics/adapters.py').read_bytes()
    return targets


def health(port,path='/health'):
    for _ in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:'+str(port)+path,timeout=3) as response:
                if response.status==200:return
        except Exception:pass
        time.sleep(1)
    raise RuntimeError('health_failed:'+str(port))


def pid_state():
    return cmd('systemctl','show','drama-material-api.service','x-post-automation.service','x-auto-post-service.service','-p','Id,MainPID')


def install():
    if cmd('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':raise RuntimeError('data_disk_missing')
    commit=cmd('git','-C',str(SOURCE),'rev-parse','HEAD')
    if cmd('git','-C',str(SOURCE),'status','--porcelain') or not cmd('git','-C',str(SOURCE),'branch','-r','--contains',commit):raise RuntimeError('verified_github_source_required')
    release=ROOT/'releases'/commit
    build(LEGACY_SOURCE,SOURCE,release)
    test_env=dict(os.environ,X_TAG_TEST_RUNTIME=str(release))
    test=cmd('/usr/bin/python3','-B',str(SOURCE/'scripts/test_x_tag_publish.py'),env=test_env,timeout=180)
    backup=ROOT/'backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');backup.mkdir(parents=True)
    targets=prepare_targets(release)
    info=dict(commit=commit,release=str(release),backup=str(backup),files={},status='prepared',legacy_pids_before=pid_state(),previous_current=str(CURRENT.resolve()) if CURRENT.exists() else None)
    for n,(target,data) in enumerate(targets.items()):
        saved=backup/str(n)
        if Path(target).exists():shutil.copy2(target,saved)
        info['files'][target]=dict(before=sha(target),after=hashlib.sha256(data).hexdigest(),backup=str(saved))
    record=backup/'deployment.json';record.write_text(json.dumps(info,indent=2))
    try:
        for name,owner,mode in [('auto','x-post-daily',0o700),('daily-work','x-post-daily',0o700),('posts','x-post-automation',0o700),('s2l','x-post-automation',0o755)]:
            path=ROOT/name;path.mkdir(exist_ok=True);shutil.chown(path,owner,owner);path.chmod(mode)
        ROOT.chmod(0o755)
        if not (ROOT/'bin').exists():(ROOT/'bin').symlink_to('/mnt/data-disk/x-post-automation/bin')
        if not ENV.exists():
            values=environment()
            atomic(ENV, ('\n'.join(k+'='+json.dumps(v,ensure_ascii=False) for k,v in sorted(values.items()))+'\n').encode(),0o600)
        else:values=read_env(ENV)
        database_presence=[(ROOT/'posts.sqlite3').exists(),(ROOT/'auto/x-auto-post.sqlite3').exists()]
        if any(database_presence) and not all(database_presence):
            raise RuntimeError('incomplete_initial_databases:inspect_bootstrap_before_retry')
        if not any(database_presence):
            bootstrap=cmd('/usr/bin/python3','-B','-m','features.x_tag_publish.bootstrap',cwd=str(release),env=dict(os.environ,**values),timeout=180)
            info['bootstrap']=json.loads(bootstrap.splitlines()[-1])
        for path in ROOT.glob('posts.sqlite3*'):shutil.chown(path,'x-post-automation','x-post-automation');path.chmod(0o600)
        for path in (ROOT/'auto').glob('*'):shutil.chown(path,'x-post-daily','x-post-daily')
        # SQLite files at the root require a writable parent for WAL/SHM creation.
        shutil.chown(ROOT,'x-post-automation','x-post-automation')
        CURRENT.parent.mkdir(parents=True,exist_ok=True)
        nxt=CURRENT.with_name('current.next');nxt.symlink_to(release);nxt.replace(CURRENT)
        for target,data in targets.items():atomic(target,data)
        cmd('systemd-tmpfiles','--create','/etc/tmpfiles.d/x-tag-publisher.conf')
        cmd('systemctl','daemon-reload')
        cmd('systemctl','enable','--now','x-tag-post.service');health(8821)
        cmd('systemctl','enable','--now','x-tag-auto.service');health(18843)
        cmd('systemctl','enable','--now','x-tag-gateway.service');health(8898,'/healthz')
        cmd('nginx','-t');cmd('systemctl','reload','nginx')
        cmd('systemctl','enable','--now',*TRIGGERS)
        info['legacy_pids_after']=pid_state();info['legacy_pids_unchanged']=info['legacy_pids_before']==info['legacy_pids_after']
        info['status']='active';record.write_text(json.dumps(info,indent=2))
    except Exception as exc:
        info['failure']=type(exc).__name__+':'+str(exc);record.write_text(json.dumps(info,indent=2))
        print(json.dumps(dict(status='install_failed',backup=str(backup),error=str(exc))),flush=True)
        rollback(backup)
        raise
    print(json.dumps(dict(status='active',commit=commit,release=str(release),backup=str(backup),legacy_pids_unchanged=info['legacy_pids_unchanged'],bootstrap=info.get('bootstrap'))))


def rollback(backup):
    backup=Path(backup).resolve()
    if backup.parent!=(ROOT/'backups').resolve():raise RuntimeError('invalid_backup')
    info=json.loads((backup/'deployment.json').read_text())
    cmd('systemctl','disable','--now',*TRIGGERS,check=False)
    # The command parks this new lane only. Never terminate an in-flight publisher.
    retained_facts=False
    for path,query in [(ROOT/'posts.sqlite3',"SELECT COUNT(*) FROM x_post_queue WHERE status='publishing'"),
                       (ROOT/'auto/x-auto-post.sqlite3',"SELECT COUNT(*) FROM x_auto_task WHERE claim_token<>''")]:
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute(query).fetchone()[0]:raise RuntimeError('new_lane_in_flight:triggers_parked;allow_current_work_to_finish_then_repeat_rollback')
                if path.name=='posts.sqlite3':retained_facts=bool(db.execute('SELECT COUNT(*) FROM x_post_publish_log').fetchone()[0])
    cmd('systemctl','stop',*(name+'.service' for name in WORKERS),timeout=60,check=False)
    cmd('systemctl','disable','--now','x-tag-gateway.service','x-tag-auto.service','x-tag-post.service',timeout=60,check=False)
    for target,row in info['files'].items():
        path=Path(target)
        if retained_facts and (path in ADAPTERS or path.name in ('gy.g2flow.com.conf','x-tag-post-s2l.conf')):continue
        current=sha(path)
        if current==row['before']:continue
        if current==row['after']:
            if row['before'] is None:path.unlink(missing_ok=True)
            else:atomic(path,Path(row['backup']).read_bytes())
        elif path.suffix=='.html' and path.name in ('x-account-list.html','x-auto-publish-templates.html','x-post-material-pool.html'):
            atomic(path,path.read_text(encoding='utf-8').replace(ENTRY,'').encode())
        elif path.name=='gy.g2flow.com.conf':atomic(path,path.read_text().replace(SHORT_INCLUDE,'').encode())
        else:raise RuntimeError('rollback_target_changed:'+target)
    cmd('systemctl','daemon-reload');cmd('nginx','-t');cmd('systemctl','reload','nginx')
    result=dict(status='rolled_back',backup=str(backup),business_databases_retained=True,publication_links_retained=retained_facts)
    (backup/'rollback.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['install','rollback']);parser.add_argument('--backup')
    args=parser.parse_args();install() if args.action=='install' else rollback(args.backup)
