"""Stage only COS destinations and exact source hosts in the selected unit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

BUCKET='ads-ai-1306474899'
HOST=BUCKET+'.cos.ap-hongkong.myqcloud.com'
OLD_BUCKETS={'advertising-1306474899','socialkit-1306474899'}
OLD_HOSTS={'advertising-1306474899.cos.ap-hongkong.myqcloud.com','socialkit-cdn.yingliang.tech'}


def properties(unit):
    raw=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','-p','ActiveState','-p','SubState','-p','TimeoutStopUSec','-p','KillMode'],universal_newlines=True)
    return dict(line.split('=',1) for line in raw.splitlines() if '=' in line)


def prepare(unit, backup, producer=False, apply=False):
    props=properties(unit);pid=int(props['MainPID'])
    if not pid: raise RuntimeError('unit is not running: '+unit)
    env=dict(line.decode().split('=',1) for line in (Path('/proc')/str(pid)/'environ').read_bytes().split(b'\0') if b'=' in line)
    values={}
    for key,value in env.items():
        if producer and key.endswith('COS_BUCKET') and value in OLD_BUCKETS: values[key]=BUCKET
        elif producer and key.endswith('COS_DOMAIN') and value.replace('https://','',1).rstrip('/')=='advertising-1306474899.cos.ap-hongkong.myqcloud.com':
            values[key]=('https://' if value.startswith('https://') else '')+HOST
        elif producer and key.endswith('COS_PREFIX') and key.startswith(('TT_POST_GPU_','FB_PAGE_GPU_')):
            values[key]=value if value.startswith('ads-ai/') else 'ads-ai/'+value.strip('/')
        elif key.endswith(('_ALLOWED_HOSTS','_SOURCE_HOSTS')):
            hosts=[h.strip() for h in value.split(',') if h.strip()]
            if HOST not in hosts:values[key]=','.join(hosts+[HOST])
    if producer and any(k.endswith('COS_BUCKET') for k in values):
        values['COS_LEGACY_BUCKET_DOMAINS']=json.dumps({'advertising-1306474899':'advertising-1306474899.cos.ap-hongkong.myqcloud.com'},separators=(',',':'))
    override=Path('/etc/ads-ai-storage')/(unit+'.env')
    dropin=Path('/etc/systemd/system')/(unit+'.d')/'zzzzz-ads-ai-storage.conf'
    row={'unit':unit,'before':props,'producer':producer,'values':values,'override':str(override),'dropin':str(dropin)}
    if apply and values:
        backup.mkdir(parents=True,exist_ok=True)
        for path in [override,dropin]:
            if path.exists():
                dest=backup/(hashlib.sha256(str(path).encode()).hexdigest()+'.before')
                if not dest.exists():shutil.copy2(path,dest)
        override.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        lines=[key+'='+json.dumps(value,ensure_ascii=False) for key,value in sorted(values.items())]
        override.write_text('\n'.join(lines)+'\n');override.chmod(0o600)
        dropin.parent.mkdir(parents=True,exist_ok=True)
        # HTTP workers already install a graceful shutdown handler; permit it to finish.
        dropin.write_text('[Service]\nEnvironmentFile='+str(override)+'\nTimeoutStopSec=infinity\n')
        (backup/(unit+'.json')).write_text(json.dumps(row,indent=2))
    return row


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('units',nargs='+');p.add_argument('--backup',type=Path,required=True)
    p.add_argument('--producer',action='store_true');p.add_argument('--apply',action='store_true');a=p.parse_args()
    for unit in a.units:print(json.dumps(prepare(unit,a.backup,a.producer,a.apply),ensure_ascii=False))
