"""Switch only the daily material report and add retained X counter fields."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time
import urllib.request

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
from scripts.deploy_social_effect_window import atomic, content, point, run, save, sha

BASE = '3dee76b310025d8bbe16264fc9c253c36360be1b'
LINKS = dict(report=Path('/opt/social-material-effects/current'),
             collector=Path('/opt/social-material-effects-collector/current'))
FILES = dict(report=['features/social_material_effects/'+f+'.py' for f in ('daily','report','window')],
             collector=['features/social_material_effects/window.py'])
ASSETS = ['x-material-effects.'+suffix for suffix in ('html','js','css')]
SERVICE = 'social-material-effects-report.service'


def stage(folder):
    if run('findmnt','-n','-o','UUID','/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('verified_data_disk_required')
    if shutil.disk_usage('/mnt/data-disk').free < 1024**3:
        raise RuntimeError('insufficient_staging_space')
    commit = run('git','-C',SRC,'rev-parse','HEAD')
    if run('git','-C',SRC,'status','--porcelain','--untracked-files=no') or not run('git','-C',SRC,'branch','-r','--contains',commit):
        raise RuntimeError('clean_fetched_source_required')
    folder.mkdir(parents=True, exist_ok=False)
    record = dict(commit=commit, source=str(SRC), status='prepared', lanes={}, assets=[])
    for lane, names in FILES.items():
        previous = LINKS[lane].resolve(strict=True)
        candidate = folder/lane
        candidate.mkdir()
        run('cp','-a','--reflink=auto',str(previous)+'/.',candidate)
        changes = {}
        for name in names:
            target = candidate/name
            if target.parent.is_symlink():
                original = target.parent.resolve()
                target.parent.unlink()
                shutil.copytree(original, target.parent, symlinks=True)
            if target.is_symlink(): target.unlink()
            if not target.resolve().is_relative_to(candidate):
                raise RuntimeError('source_symlink_escape')
            before = previous/name
            # The serving report predates window.py; daily.py is new everywhere.
            raw = (SRC/name).read_bytes() if lane == 'report' and not before.exists() else content(SRC,BASE,name,before)
            compile(raw,name,'exec')
            atomic(target,raw)
            changes[name] = dict(before=sha(before), after=sha(target))
        record['lanes'][lane] = dict(previous=str(previous),candidate=str(candidate),files=changes)
    assets = folder/'assets'
    assets.mkdir()
    for root in (Path('/root/drama_material_service/static'), Path('/usr/share/nginx/html')):
        for name in ASSETS:
            original = root/name
            target = original.resolve(strict=True)
            if any(a['target'] == str(target) for a in record['assets']): continue
            ident = str(len(record['assets']))
            before, after = assets/(ident+'.before'), assets/(ident+'.after')
            shutil.copy2(target,before)
            after.write_bytes(content(SRC,BASE,'static/'+name,target))
            record['assets'].append(dict(path=str(original),target=str(target),id=ident,before=sha(before),after=sha(after)))
            candidate_asset = folder/'report/static'/name
            if candidate_asset.is_symlink(): candidate_asset.unlink()
            if not candidate_asset.resolve().is_relative_to(folder/'report'):
                raise RuntimeError('static_symlink_escape')
            atomic(candidate_asset,after.read_bytes())
    save(folder/'manifest.json',record)


def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(20):
        try:
            with opener.open('http://127.0.0.1:18989/healthz',timeout=3) as response:
                if json.loads(response.read())['ok']: return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError('report_health_failed')


def switch(folder, reverse=False):
    record = json.loads((folder/'manifest.json').read_text())
    for lane, info in record['lanes'].items():
        if lane == 'collector' and reverse: continue  # Preserve added observations.
        if str(LINKS[lane].resolve()) not in (info['previous'],info['candidate']):
            raise RuntimeError('later_release:'+lane)
        for name, hashes in info['files'].items():
            if sha(Path(info['candidate'])/name) != hashes['after']:
                raise RuntimeError('candidate_changed:'+name)
            if sha(Path(info['previous'])/name) != hashes['before']:
                raise RuntimeError('previous_release_changed:'+name)
    for asset in record['assets']:
        if str(Path(asset['path']).resolve()) != asset['target'] or sha(Path(asset['target'])) not in (asset['before'],asset['after']):
            raise RuntimeError('concurrent_static_change:'+asset['path'])
    if not reverse:
        point(LINKS['collector'],record['lanes']['collector']['candidate'])
    report = record['lanes']['report']
    destination = report['previous' if reverse else 'candidate']
    point(LINKS['report'],destination)
    run('systemctl','restart',SERVICE)
    health()
    for asset in record['assets']:
        atomic(Path(asset['target']), (folder/'assets'/(asset['id']+('.before' if reverse else '.after'))).read_bytes())
    record['status'] = 'rolled_back' if reverse else 'active'
    save(folder/'manifest.json',record)
    print(json.dumps(dict(status=record['status'],commit=record['commit'],report=destination,
                          collector=str(LINKS['collector'].resolve()),health='ok')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage','activate','rollback'))
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if not folder.is_relative_to('/mnt/data-disk') or not Path('/mnt/data-disk').is_mount():
        raise ValueError('mounted_data_disk_required')
    if args.action == 'stage': stage(folder)
    else: switch(folder, args.action == 'rollback')
