"""Stop future refreshes or return reads to the verified CPU snapshot."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def update(path,changes):
    path=Path(path);value=json.loads(path.read_text());info=path.stat()
    temporary=path.with_suffix('.rollback-next')
    value.update(changes)
    with temporary.open('w') as out:
        json.dump(value,out);out.flush();os.fsync(out.fileno())
    os.chmod(str(temporary),info.st_mode & 0o777)
    os.chown(str(temporary),info.st_uid,info.st_gid)
    temporary.replace(path)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('side',choices=['hk','cpu']);side=parser.parse_args().side
    if side=='hk':
        update('/data/ai-backend-offload/campaign/schedule.json',{'enabled':False})
        subprocess.run(['systemctl','stop','ai-backend-offload-campaign-refresh.timer'],check=True)
        print('Future Campaign refreshes paused; any current refresh continues.')
    else:
        path=Path('/etc/ai-backend-offload/campaign-gateway.json')
        config=json.loads(path.read_text())
        if config.get('origin_reader_port')!=19978:raise RuntimeError('verified_cpu_cache_route_required')
        if subprocess.check_output(['systemctl','is-active','ai-backend-offload-campaign-local.service']).strip()!=b'active':
            raise RuntimeError('cpu_snapshot_reader_unavailable')
        manifest=Path('/mnt/data-disk/gpu-backend-offload-20261123/campaign-local/index/current.json')
        if not json.loads(manifest.read_text()).get('version'):raise RuntimeError('verified_cpu_snapshot_required')
        update(path,{'percentage':0})
        print('Campaign reads use the CPU immutable snapshot; no SQL refresher started.')


if __name__=='__main__':main()
