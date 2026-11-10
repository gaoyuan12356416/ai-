"""Deploy a verified narrow FB query overlay; preserve runtime overlays and ledgers."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import deploy_fb_skipped_recheck as ops
from scripts.deploy_pipeline_runtime import preserve_tree_owners, atomic_copy, verify_source

BASE = '23f615debb88a0c14a93aa84d6a11e2b91f75c71'
NATIVE = {'features/pipeline_dispatch/fb_native.py', 'features/fb_video_publish/group_sources.py',
          'features/fb_video_publish/identity_client.py'}
CENTRAL = {'features/pipeline_dispatch/core.py', 'features/pipeline_dispatch/service.py'}
POOL_OVERLAY = '42c3ef04b76cfbc3ef97504b474fda51d7412be08227cb90f62425d2f329d846'
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'

def scheduler(action, folder, suffix):
    for lane in ('fb', 'coordinator'):
        args = [sys.executable, HEALTH, action, '--lane', lane] if action == 'snapshot' else [sys.executable, HEALTH, action]
        if action == 'restore':
            args += ['--baseline', str(folder/(lane+'-schedulers-before.json')), '--wait-seconds', '25']
        args += ['--output', str(folder/(lane+'-schedulers-'+suffix+'.json'))]
        ops.run(args, timeout=55)

def prepare(source, commit, folder):
    commit = verify_source(source, commit)
    assert ops.DATA.is_mount() and folder.is_relative_to(ops.DATA)
    assert ops.run(['findmnt', '-n', '-o', 'UUID', str(ops.DATA)]) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage(ops.DATA).free > 3 * 1024**3, 'insufficient_data_disk_space'
    changed = set(ops.run(['git', 'diff', '--name-only', BASE, commit, '--', 'features'], cwd=source).splitlines())
    assert changed == NATIVE | CENTRAL, 'unexpected_overlay_scope'
    folder.mkdir(mode=0o755, parents=True, exist_ok=False)
    manifest = {'commit':commit, 'source':str(source), 'baseline':BASE, 'status':'preparing', 'lanes':{}}
    for lane, (root, unit, port) in ops.LANES.items():
        assert root.is_symlink()
        target = folder/'candidates'/lane
        target.parent.mkdir(exist_ok=True)
        shutil.copytree(root.resolve(), target, symlinks=True, ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
        preserve_tree_owners(root.resolve(), target)
        item = {'previous_root':str(root.resolve()), 'candidate':str(target), 'files':{}}
        manifest['lanes'][lane] = item
        for relative in sorted(NATIVE | CENTRAL if lane == 'coordinator' else NATIVE):
            live = root/relative
            original = subprocess.check_output(['git','show',BASE+':'+relative], cwd=source)
            assert live.read_bytes().replace(b'\r\n', b'\n') == original or (relative.endswith('/fb_native.py') and ops.digest(live) == POOL_OVERLAY), 'unreviewed_live_baseline:'+lane+'/'+relative
            assert (target/relative).resolve().is_relative_to(target.resolve())
            before = ops.digest(live)
            atomic_copy(source/relative, target/relative)
            compile((target/relative).read_bytes(), relative, 'exec')
            item['files'][relative] = {'before':before, 'after':ops.digest(target/relative)}
    ops.verify_live(manifest)
    ops.backup(ops.FB_DB, folder/'fb-before.sqlite3')
    ops.backup(ops.DISPATCH_DB, folder/'dispatch-before.sqlite3')
    manifest['status'] = 'prepared'
    ops.save(folder/'manifest.json', manifest)
    print(json.dumps({'prepared':True, 'commit':commit, 'files':{k:len(v['files']) for k,v in manifest['lanes'].items()}}))

def activate(folder):
    scheduler('snapshot', folder, 'before')
    ops.apply(folder)
    scheduler('restore', folder, 'after')

def rollback(folder):
    original = json.loads((folder/'manifest.json').read_text())
    rollback_dir = folder/'rollback'; rollback_dir.mkdir(mode=0o755)
    manifest = {**original, 'status':'prepared', 'lanes':{}}
    for lane, item in original['lanes'].items():
        assert str(ops.LANES[lane][0].resolve()) == item['candidate'], 'later_release_present'
        manifest['lanes'][lane] = {'previous_root':item['candidate'], 'candidate':item['previous_root'],
            'files':{p:{'before':v['after'],'after':v['before']} for p,v in item['files'].items()}}
    ops.save(rollback_dir/'manifest.json', manifest)
    activate(rollback_dir)
    print(json.dumps({'rolled_back':True, 'business_ledgers_preserved':True}))

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['prepare','apply','rollback']);p.add_argument('--folder',type=Path,required=True)
    p.add_argument('--source',type=Path);p.add_argument('--commit');a=p.parse_args()
    if a.action == 'prepare':
        prepare(a.source.resolve(),a.commit,a.folder.resolve())
    elif a.action == 'apply':
        activate(a.folder.resolve())
    else:
        rollback(a.folder.resolve())
