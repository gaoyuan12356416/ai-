"""Static-only pool recovery update. No service, ledger or timer changes."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

import deploy_x_personal_publish as base

ROOT = Path(__file__).resolve().parents[1]
FILES = ['x-video-account-settings.js', 'x-account-list.html', 'x-video-publish.html']
BASE = 'a9fc7f4'
REF = 'refs/heads/codex/x-pool-import-fix-20261027'


def public_check(entries, rollback):
    for item in entries:
        if '/usr/share/nginx/html/' not in item['target']:
            continue
        with urllib.request.urlopen('https://ai.yingliangads.com/' + item['name'] + '?v=20261027pool-import', timeout=15) as response:
            import hashlib
            base.require(hashlib.sha256(response.read()).hexdigest() == item['before' if rollback else 'after'], 'Public static hash mismatch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rollback', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    config = base.load_baseline()
    targets = [(name, Path(config['paths'][key]) / ('static' if key != 'public' else '') / name)
               for name in FILES for key in ('main', 'current', 'public')]
    with base.deployment_lock(config):
        if args.rollback:
            manifest = json.loads(args.rollback.read_text())
            base.require({i['target'] for i in manifest['files']} == {str(t) for _,t in targets}, 'Rollback scope mismatch')
        else:
            commit = base.run('git','-C',str(ROOT),'rev-parse','HEAD')
            base.require(not base.run('git','-C',str(ROOT),'status','--porcelain'), 'Dirty checkout')
            base.require(base.run('git','-C',str(ROOT),'ls-remote','origin',REF).split() == [commit,REF], 'Unpushed commit')
            for name,target in targets:
                expected = subprocess.check_output(['git','-C',str(ROOT),'show',BASE+':static/'+name])
                base.require(target.read_bytes().replace(b'\r\n',b'\n') == expected, 'Static baseline drift: '+str(target))
            if args.check:
                print(json.dumps({'files':len(targets),'baseline_verified':True,'commit':commit}));return
            backup = Path(config['paths']['data'])/'backups'/(time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-pool-ui-'+commit[:8])
            backup.mkdir(mode=0o700)
            manifest = {'backup':str(backup),'commit':commit,'status':'prepared','files':[]}
            for index,(name,target) in enumerate(targets):
                saved=backup/('file-'+str(index));shutil.copy2(target,saved)
                manifest['files'].append({'name':name,'target':str(target),'backup':str(saved),'before':base.digest(target),'after':base.digest(ROOT/'static'/name)})
        base.save(manifest)
        for item in manifest['files']:
            base.require(base.digest(Path(item['target'])) == item['after' if args.rollback else 'before'], 'Live static drift')
        try:
            for item in manifest['files']:
                base.atomic_copy(Path(item['backup']) if args.rollback else ROOT/'static'/item['name'],Path(item['target']))
            public_check(manifest['files'],bool(args.rollback))
            manifest['status']='rolled_back' if args.rollback else 'deployed'
        except Exception:
            manifest['status']='recovery_required';base.save(manifest)
            for item in manifest['files']:
                base.atomic_copy(ROOT/'static'/item['name'] if args.rollback else Path(item['backup']),Path(item['target']))
            public_check(manifest['files'],not bool(args.rollback))
            manifest['status']='failed_restored';base.save(manifest)
            raise
        base.save(manifest)
        print(json.dumps({'status':manifest['status'],'manifest':str(Path(manifest['backup'])/'manifest.json'),'commit':manifest['commit']}))


if __name__=='__main__':
    main()
