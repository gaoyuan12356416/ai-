"""Switch only the public read-only monitor summary while the coordinator is busy."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time


ROOT = Path('/mnt/data-disk/gpu-backend-offload-20261123')
ETC = Path('/etc/ai-backend-offload')
ROUTE = Path('/etc/nginx/default.d/ai-backend-offload.conf')
UNIT = 'ai-backend-offload-gateway@fbclock.service'
PORT = 19988
BASE = 'bcf6996a0297589ef119fb55c02fa5f7db23056f'


def run(*args):
    return subprocess.check_output(list(args), stderr=subprocess.STDOUT, text=True, timeout=45).strip()


def prop(unit, key):
    return run('systemctl', 'show', unit, '-p', key, '--value')


def replace_route(old_port, new_port):
    before = ROUTE.read_bytes()
    prefix = b'location = /api/pipeline-monitor/summary {\n    proxy_pass http://127.0.0.1:'
    old = prefix + str(old_port).encode() + b';'
    assert before.count(old) == 1, 'the live summary route has changed'
    updated = before.replace(old, prefix + str(new_port).encode() + b';')
    def write(content):
        tmp = ROUTE.with_suffix('.fbclock-next')
        assert not tmp.exists()
        shutil.copy2(ROUTE, tmp)
        tmp.write_bytes(content)
        os.replace(tmp, ROUTE)
    write(updated)
    try:
        run('nginx','-t')
        run('systemctl','reload','nginx')
    except BaseException:
        write(before)
        run('nginx','-t')
        run('systemctl','reload','nginx')
        raise
    return hashlib.sha256(updated).hexdigest()


def anonymous(port):
    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    try:
        conn.request('GET','/api/pipeline-monitor/summary')
        reply=conn.getresponse()
        reply.read()
        return reply.status
    finally:
        conn.close()


def switch(release, rollback):
    blue=ROOT/'gateway-blue-current'
    assert blue.resolve().name==BASE
    blue_unit='ai-backend-offload-gateway@blue.service'
    assert prop(blue_unit,'ActiveState')=='active' and anonymous(19964)==401
    folder=ROOT/'repairs'/('fb-summary-'+release.name[:12])
    folder.mkdir(mode=0o700,exist_ok=True)
    if rollback:
        assert (folder/'deployment.json').exists()
        checksum=replace_route(PORT,19964)
        run('systemctl','disable',UNIT)
        return {'reverted':True,'route_sha256':checksum}
    assert re.fullmatch('[0-9a-f]{40}',release.name)
    assert prop(UNIT,'MainPID')=='0'
    for unit in ('pipeline-coordinator','pipeline-node-cpu-main','drama-material-api'):
        assert prop(unit,'ActiveState')=='active'
    pids={u:prop(u,'MainPID') for u in ('pipeline-coordinator','pipeline-node-cpu-main','drama-material-api')}
    template=Path('/etc/systemd/system/ai-backend-offload-gateway@.service')
    assert template.read_bytes()==(blue/'deploy/ai-backend-offload-gateway@.service').read_bytes()
    with socket.socket() as s:
        s.bind(('127.0.0.1',PORT))
    link=ROOT/'gateway-fbclock-current'
    if not link.exists():link.symlink_to(release)
    assert link.resolve()==release
    source=(ETC/'gateway-blue.env').read_text()
    modified,n=re.subn(r'^OFFLOAD_GATEWAY_PORT=.*$', 'OFFLOAD_GATEWAY_PORT='+str(PORT),source,flags=re.MULTILINE)
    assert n==1
    env=ETC/'gateway-fbclock.env'
    if env.exists():assert env.read_text()==modified
    else:
        with env.open('x') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(modified)
    previous=folder/'route-before.conf'
    if not previous.exists():shutil.copy2(ROUTE,previous)
    run('systemctl','start',UNIT)
    for _ in range(30):
        try:
            if anonymous(PORT)==401:break
        except OSError:pass
        time.sleep(.2)
    else:raise RuntimeError('summary gateway did not become ready')
    run('systemctl','enable',UNIT)
    checksum=replace_route(19964,PORT)
    record={'release':release.name,'guarded_route':'/api/pipeline-monitor/summary','sha256':checksum,
            'unit':UNIT,'unit_pid':prop(UNIT,'MainPID'),'protected_pids_before':pids,
            'protected_pids_after':{u:prop(u,'MainPID') for u in pids},'anonymous':anonymous(PORT),
            'rollback':'/usr/bin/python3 '+str(release/'scripts/switch_fb_summary_gateway.py')+' --rollback'}
    (folder/'deployment.json').write_text(json.dumps(record))
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--rollback',action='store_true')
    print(json.dumps(switch(Path(__file__).resolve().parents[1],parser.parse_args().rollback)))
