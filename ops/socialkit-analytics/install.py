"""Scoped deployment; run only from the verified GitHub release on the CPU host."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

DATA = Path('/mnt/data-disk/socialkit-analytics')
RUNTIME = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
HERE = Path(__file__).resolve().parent
RELEASE = HERE.parents[1]
MOUNT_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'


def check_mount():
    value = subprocess.check_output(['findmnt','-n','-o','UUID','/mnt/data-disk'],text=True).strip()
    if value != MOUNT_UUID:
        raise RuntimeError('Expected data disk is not mounted')
    if shutil.disk_usage('/mnt/data-disk').free < 2 * 1024**3:
        raise RuntimeError('Insufficient data disk space')


def atomic_write(path, content, mode=0o644):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'.',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(content);f.flush();os.fsync(f.fileno())
        os.chmod(name,mode);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def envfile(path):
    out={}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1);out[key.strip()]=value.strip().strip('"').strip("'")
    return out


def prepare_env():
    pid=subprocess.check_output(['systemctl','show','drama-material-api.service','-p','MainPID','--value'],text=True).strip()
    current=dict(v.split('=',1) for v in Path('/proc/'+pid+'/environ').read_text().split('\0') if '=' in v)
    source=envfile('/etc/socialkit-tiktok-account-sync.env')
    result={k:v for k,v in current.items() if k.startswith('ADMIN_MAPPING_MYSQL_')}
    result.update({k:v for k,v in source.items() if k.startswith('SOCIALKIT_TT_SOURCE_MYSQL_')})
    for suffix in ('HOST','PORT','USER','PASSWORD','DATABASE'):
        key='SOCIALKIT_TT_TARGET_MYSQL_'+suffix
        if key in source:result['SOCIALKIT_ANALYTICS_WRITE_MYSQL_'+suffix]=source[key]
    result['SOCIALKIT_ANALYTICS_WRITE_MYSQL_DATABASE']='ads_ai'
    result['SOCIALKIT_ANALYTICS_ROOT']=str(DATA)
    assert result['ADMIN_MAPPING_MYSQL_HOST']=='101.32.56.53' and result['ADMIN_MAPPING_MYSQL_PORT']=='63350'
    assert result['SOCIALKIT_ANALYTICS_WRITE_MYSQL_HOST']=='101.32.56.53' and result['SOCIALKIT_ANALYTICS_WRITE_MYSQL_PORT']=='63353'
    assert result['SOCIALKIT_TT_SOURCE_MYSQL_HOST']=='hk-cynosdbmysql-grp-dejftnkp.sql.tencentcdb.com'
    # systemd EnvironmentFile: double quote and escape without shell evaluation.
    def quote(v):return '"'+str(v).replace('\\','\\\\').replace('"','\\"').replace('`','\\`').replace('$','\\$')+'"'
    content='\n'.join(k+'='+quote(v) for k,v in sorted(result.items()))+'\n'
    atomic_write(Path('/etc/socialkit-analytics.env'),content.encode(),0o600)
    print('Configured private report environment; credential values suppressed.')


def migrate():
    import pymysql
    # systemd passes correctly decoded environment; do not reparse escaped secrets.
    e=os.environ;p='SOCIALKIT_ANALYTICS_WRITE_MYSQL_'
    assert e[p+'HOST']=='101.32.56.53' and e[p+'PORT']=='63353' and e[p+'DATABASE']=='ads_ai'
    statements=[v.strip() for v in (HERE/'migration.sql').read_text().split(';') if v.strip()]
    import re
    names=[]
    for statement in statements:
        without_comments=re.sub(r'(?m)^\s*--.*$','',statement).strip()
        match=re.match(r'CREATE TABLE IF NOT EXISTS (?:`?ads_ai`?\.)?`?(socialkit_effect_daily|socialkit_publish_daily|socialkit_refresh_runs)`?\s*\(',without_comments,re.I)
        if not match:raise RuntimeError('Migration contains an unexpected statement')
        names.append(match.group(1))
    assert set(names)=={'socialkit_effect_daily','socialkit_publish_daily','socialkit_refresh_runs'} and len(names)==3
    connection=pymysql.connect(host=e[p+'HOST'],port=63353,user=e[p+'USER'],password=e[p+'PASSWORD'],database='ads_ai',connect_timeout=3,read_timeout=30,write_timeout=10,charset='utf8mb4',autocommit=True)
    try:
        with connection.cursor() as c:
            c.execute('SELECT DATABASE(),@@read_only');identity=c.fetchone()
            assert identity==('ads_ai',0)
            for statement in statements:c.execute(statement)
    finally:connection.close()
    print('Migration verified/applied for three SocialKit tables only.')


def install(commit):
    actual=subprocess.check_output(['git','-C',str(RELEASE),'rev-parse','HEAD'],text=True).strip()
    if actual!=commit:raise RuntimeError('Release commit mismatch')
    subprocess.run(['git','-C',str(RELEASE),'diff','--exit-code','HEAD'],check=True,stdout=subprocess.DEVNULL)
    spec=importlib.util.spec_from_file_location('socialkit_integration',HERE/'integrate.py')
    patch=importlib.util.module_from_spec(spec);spec.loader.exec_module(patch)
    # Build and validate all narrow patches before changing any running file.
    writes={RUNTIME/'app.py':patch.patch_app((RUNTIME/'app.py').read_text()).encode()}
    for dest in [RUNTIME/'static/quick-nav.js',WEB/'quick-nav.js']:
        writes[dest]=patch.patch_nav(dest.read_text()).encode()
    for src in (RELEASE/'features/socialkit_analytics').glob('*.py'):
        writes[RUNTIME/'features/socialkit_analytics'/src.name]=src.read_bytes()
    for name in ('socialkit-analytics.html','socialkit-analytics.js','socialkit-analytics.css'):
        content=(RELEASE/'static'/name).read_bytes()
        writes[RUNTIME/'static'/name]=content;writes[WEB/name]=content
    for name in ('socialkit-analytics-refresh.service','socialkit-analytics-refresh.timer'):
        writes[Path('/etc/systemd/system')/name]=(HERE/name).read_bytes()
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=DATA/'backups'/stamp;backup.mkdir(parents=True,exist_ok=False);os.chmod(backup,0o700)
    manifest={'commit':commit,'release':str(RELEASE),'files':[],'old_current':None}
    current=Path('/opt/socialkit-analytics/current')
    if current.is_symlink():manifest['old_current']=os.readlink(current)
    for i,(dest,content) in enumerate(writes.items()):
        item={'path':str(dest),'existed':dest.exists(),'backup':str(i)}
        if dest.exists():
            item['mode']=dest.stat().st_mode & 0o777;shutil.copy2(dest,backup/str(i))
            item['old_sha256']=hashlib.sha256(dest.read_bytes()).hexdigest()
        item['new_sha256']=hashlib.sha256(content).hexdigest();manifest['files'].append(item)
    atomic_write(backup/'manifest.json',json.dumps(manifest,indent=2).encode(),0o600)
    for dest,content in writes.items():atomic_write(dest,content)
    current.parent.mkdir(parents=True,exist_ok=True)
    temp=current.with_name('.current-'+stamp);os.symlink(str(RELEASE),temp);os.replace(temp,current)
    subprocess.run(['python3','-m','py_compile',str(RUNTIME/'app.py')],check=True)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    print(json.dumps({'commit':commit,'backup':str(backup),'release':str(RELEASE),'restart_required':'drama-material-api.service','timer_enabled':False}))


def rollback(manifest_path):
    manifest_path=Path(manifest_path).resolve()
    if DATA/'backups' not in manifest_path.parents:raise ValueError('Rollback manifest outside report backups')
    manifest=json.loads(manifest_path.read_text());backup=manifest_path.parent
    for entry in manifest['files']:
        dest=Path(entry['path'])
        # Avoid overwriting later unrelated edits; changes must still match this release.
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()!=entry['new_sha256']:
            raise RuntimeError('Later file change prevents automatic rollback: '+str(dest))
    subprocess.run(['systemctl','disable','--now','socialkit-analytics-refresh.timer'],check=False)
    for entry in manifest['files']:
        dest=Path(entry['path'])
        if entry['existed']:atomic_write(dest,(backup/entry['backup']).read_bytes(),entry.get('mode',0o644))
        elif dest.exists():dest.unlink()
    current=Path('/opt/socialkit-analytics/current')
    if manifest['old_current']:
        temp=current.with_name('.current-rollback');os.symlink(manifest['old_current'],temp);os.replace(temp,current)
    elif current.is_symlink():current.unlink()
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','restart','drama-material-api.service'],check=True)
    print('Code restored; report facts and snapshots preserved.')


def rollback_snapshot():
    import fcntl
    # Serialize with the refresh service; never switch a version during publication.
    with open(DATA/'refresh.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current=json.loads((DATA/'current.json').read_text())
        previous=json.loads((DATA/'previous.json').read_text())
        for value in (current,previous):
            path=(DATA/value['database']).resolve()
            if DATA.resolve() not in path.parents or not path.is_file():
                raise RuntimeError('Rollback snapshot is not available')
        if current['version']==previous['version']:
            raise RuntimeError('No distinct previous snapshot')
        atomic_write(DATA/'previous.json',json.dumps(current,ensure_ascii=False).encode())
        atomic_write(DATA/'current.json',json.dumps(previous,ensure_ascii=False).encode())
        print(json.dumps({'current_version':previous['version'],'previous_version':current['version']}))


def main():
    parser=argparse.ArgumentParser();group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare-env',action='store_true');group.add_argument('--migrate',action='store_true');group.add_argument('--install',metavar='COMMIT');group.add_argument('--rollback',metavar='MANIFEST');group.add_argument('--rollback-snapshot',action='store_true')
    args=parser.parse_args();check_mount();DATA.mkdir(exist_ok=True)
    if args.prepare_env:prepare_env()
    elif args.migrate:migrate()
    elif args.install:install(args.install)
    elif args.rollback_snapshot:rollback_snapshot()
    else:rollback(args.rollback)


if __name__=='__main__':main()
