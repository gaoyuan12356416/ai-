"""Apply/rollback the bounded source repair without replacing publisher state."""
import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import deploy_x_personal_publish as base

ROOT = Path(__file__).resolve().parents[1]
BASE = 'd35d455'
REF = 'refs/heads/codex/x-personal-source-fix-20261023'
FILES = ['features/x_video_publish/sources.py', 'features/x_posts/selector.py',
         'static/x-video-account-settings.js', 'static/x-account-list.html', 'static/x-video-publish.html']
UNITS = ['drama-material-api.service', 'x-post-automation.service']
TABLES = ['x_post_queue', 'x_post_publish_log', 'x_post_manual_run',
          'x_video_publish_batch', 'x_video_publish_task', 'x_video_publish_pool',
          'x_video_publish_pool_item', 'x_video_publish_pool_slot', 'x_video_publish_pool_operation']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def targets(config):
    result = []
    for name in FILES:
        for key in ('main', 'current'):
            result.append((name, Path(config['paths'][key]) / name))
        if name.startswith('static/'):
            result.append((name, Path(config['paths']['public']) / Path(name).name))
    return result


def window(config):
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    with closing(sqlite3.connect(Path(config['paths']['post_db']).resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        for raw, in conn.execute("SELECT times_json FROM x_video_publish_pool WHERE status='enabled'"):
            for value in json.loads(raw):
                h,m = map(int,value.split(':'))
                slot = now.replace(hour=h,minute=m,second=0,microsecond=0)
                for offset in (-1,0,1):
                    delta = (slot+timedelta(days=offset)-now).total_seconds()
                    base.require(not -60 <= delta <= 300, 'Pool slot nearby; retry outside trigger window')


def change(config, manifest, rollback=False):
    window(config)
    with base.paused(config, manifest):
        window(config)
        pool_state = base.unit_state(base.POOL)
        manifest['pool_state'] = pool_state
        base.save(manifest)
        if pool_state['ActiveState']=='active':
            base.run('systemctl','stop',base.POOL)
        modified = False
        try:
            base.run('systemctl','stop',*UNITS)
            db = Path(config['paths']['post_db'])
            base.snapshot(db,Path(manifest['backup'])/('before-rollback.sqlite3' if rollback else 'before.sqlite3'))
            facts = base.database_facts(db,tables=TABLES)
            token_facts = base.token_facts(config)
            if not rollback:
                shutil.copytree(config['paths']['tokens'],Path(manifest['backup'])/'tokens')
                (Path(manifest['backup'])/'tokens').chmod(0o700)
            manifest['ledger_before'] = facts['tables']
            base.save(manifest)
            for item in manifest['files']:
                target = Path(item['target'])
                expected = item['after'] if rollback else item['before']
                base.require(base.digest(target)==expected,'Live file changed: '+str(target))
                source = Path(item['backup']) if rollback else ROOT/item['relative']
                base.require(base.digest(source)==(item['before'] if rollback else item['after']),'Source hash mismatch')
            modified = True
            for item in manifest['files']:
                source = Path(item['backup']) if rollback else ROOT/item['relative']
                base.atomic_copy(source,Path(item['target']))
            for item in manifest['files']:
                base.require(base.digest(Path(item['target']))==(item['before'] if rollback else item['after']),'Installed hash mismatch')
            base.run('systemctl','start',*UNITS)
            base.health(config)
            after = base.database_facts(db,projection=facts['columns'])
            base.require(after==facts,'Publisher ledger changed during maintenance')
            base.require(base.token_facts(config)==token_facts,'Token state changed during maintenance')
            manifest['status']='rolled_back' if rollback else 'deployed'
            manifest['ledger_preserved']=True
            manifest['tokens_preserved']=True
            base.save(manifest)
        except Exception:
            manifest['status']='recovery_required'
            base.save(manifest)
            if modified:
                base.run('systemctl','stop',*UNITS)
                for item in manifest['files']:
                    # Restore the immediately preceding code; never restore a DB.
                    source = ROOT/item['relative'] if rollback else Path(item['backup'])
                    base.atomic_copy(source,Path(item['target']))
            base.run('systemctl','start',*UNITS)
            base.health(config)
            manifest['status']='failed_restored'
            base.save(manifest)
            raise
        finally:
            if manifest.get('status')!='recovery_required' and pool_state['ActiveState']=='active':
                base.run('systemctl','start',base.POOL)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--rollback',type=Path)
    p.add_argument('--check',action='store_true')
    args=p.parse_args()
    config=base.load_baseline()
    with base.deployment_lock(config):
        if args.rollback:
            manifest=json.loads(args.rollback.read_text())
            allowed={str(t) for _,t in targets(config)}
            base.require({i['target'] for i in manifest['files']}==allowed,'Rollback scope mismatch')
            change(config,manifest,True)
        else:
            commit=base.run('git','-C',str(ROOT),'rev-parse','HEAD')
            base.require(not base.run('git','-C',str(ROOT),'status','--porcelain'),'Dirty source checkout')
            base.require(base.run('git','-C',str(ROOT),'ls-remote','origin',REF).split()==[commit,REF],'Commit not exact GitHub ref')
            entries=[]
            for name,target in targets(config):
                expected=subprocess.check_output(['git','-C',str(ROOT),'show',BASE+':'+name])
                base.require(base.digest(target,'lf')==sha(expected.replace(b'\r\n',b'\n')),'Baseline drift: '+str(target))
                if name.endswith('.py'):
                    compile((ROOT/name).read_bytes(),name,'exec')
                entries.append((name,target))
            window(config)
            if args.check:
                print(json.dumps({'baseline_verified':True,'files':len(entries),'commit':commit}));return
            stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-source-'+commit[:8]
            backup=Path(config['paths']['data'])/'backups'/stamp
            backup.mkdir(mode=0o700)
            manifest={'version':1,'backup':str(backup),'commit':commit,'status':'prepared','files':[]}
            for index,(name,target) in enumerate(entries):
                saved=backup/('file-'+str(index));shutil.copy2(target,saved)
                manifest['files'].append({'relative':name,'target':str(target),'backup':str(saved),
                                          'before':base.digest(target),'after':base.digest(ROOT/name)})
            base.save(manifest)
            change(config,manifest)
        print(json.dumps({'status':manifest['status'],'manifest':str(Path(manifest['backup'])/'manifest.json'),
                          'commit':manifest['commit'],'files':len(manifest['files'])}))


if __name__=='__main__':
    main()
