"""Install only the report proxy from the verified GitHub release; no API restart."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
import urllib.request

BASE=Path('/mnt/data-disk/deploy/youtube-auto-publish')
TARGET=Path('/etc/nginx/default.d/youtube-analytics.conf')


def run(*args):
    return subprocess.check_output(args,stderr=subprocess.STDOUT,text=True).strip()


def digest(data):return hashlib.sha256(data).hexdigest()


def atomic(data):
    temp=TARGET.with_suffix('.analytics-new')
    temp.write_bytes(data);os.chmod(temp,0o644);os.replace(temp,TARGET)


def check_http(expected):
    try:
        with urllib.request.urlopen('https://ai.yingliangads.com/api/youtube-analytics/options',timeout=15) as response:
            status=response.status
    except urllib.error.HTTPError as exc:status=exc.code
    if status!=expected:raise RuntimeError('Public anonymous route unexpected status: '+str(status))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--rollback',type=Path);args=parser.parse_args()
    os.umask(0o077)
    if run('findmnt','-n','-o','UUID','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data disk mount mismatch')
    if args.rollback:
        backup=args.rollback.resolve()
        if (BASE/'backups').resolve() not in backup.parents:raise RuntimeError('Invalid backup directory')
        manifest=json.loads((backup/'proxy-manifest.json').read_text())
        if not TARGET.is_file() or digest(TARGET.read_bytes())!=manifest['installed_sha256']:
            raise RuntimeError('Refuse rollback over later proxy changes')
        old=TARGET.read_bytes();TARGET.unlink()
        try:
            run('nginx','-t');run('systemctl','reload','nginx')
        except Exception:
            atomic(old);run('nginx','-t');run('systemctl','reload','nginx');raise
        print(json.dumps(dict(proxy_rollback='complete',backup=str(backup))))
        return
    stage=Path(__file__).resolve().parents[1]
    commit=run('git','-C',str(stage),'rev-parse','HEAD')
    if run('git','-C',str(stage),'status','--porcelain'):raise RuntimeError('Dirty release')
    if TARGET.exists():raise RuntimeError('Proxy already exists; inspect before changing')
    data=(stage/'deploy/youtube-analytics.nginx.conf').read_bytes()
    backup=BASE/'backups'/('analytics-proxy-'+time.strftime('%Y%m%d-%H%M%S')+'-'+commit[:12]);backup.mkdir(parents=True)
    # This is additive; retain the effective before-config for an auditable rollback.
    (backup/'nginx-before.txt').write_text(run('nginx','-T'))
    (backup/'proxy-manifest.json').write_text(json.dumps(dict(commit=commit,target=str(TARGET),previous=None,installed_sha256=digest(data)),indent=2))
    atomic(data)
    try:
        run('nginx','-t');run('systemctl','reload','nginx');check_http(401)
    except Exception:
        TARGET.unlink();run('nginx','-t');run('systemctl','reload','nginx');raise
    print(json.dumps(dict(proxy='installed',commit=commit,backup=str(backup),anonymous_status=401,api_restart=False)))


if __name__=='__main__':main()
