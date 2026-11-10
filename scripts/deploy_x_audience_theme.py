#!/usr/bin/env python3
"""Scoped two-level portrait switch, preserving the current publishing pipeline."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts.deploy_x_hierarchy import mount_check, source_commit, switch_window, switch_current, reload_services
from scripts.deploy_x_tag_revenue_read import atomic, save, file_sha, sha, parents
from scripts.deploy_x_tag_pool_fix import state
from features.x_tag_revenue.hierarchy import VERSION, rebuild_profile
from features.x_tag_revenue.integration import patch_two_levels
from features.x_tag_revenue.storage import ProfileWriter
from features.x_tag_revenue.serving import publish, Store
from features.social_analytics.warehouse_refresh import Reader, environment, atomic_json

BASE = 'f26df6ba0e34aaa87b2224d9a6112051298d8115'
OLD_RUNTIME = '79b2b601349bdc2045ab5a1ca3611ec996808dd0'
ROOT = Path('/mnt/data-disk/x-channel-tag-revenue')
MAIN = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
CURRENT = Path('/opt/x-tag-publisher/current')
BACKUPS = ROOT / 'audience-theme-backups'
MODULES = ['features/x_tag_revenue/' + n for n in ('hierarchy.py', 'storage.py', 'serving.py', 'refresh.py', 'routes.py', 'integration.py')]
POLICY = 'features/x_tag_publish/policy.py'
ASSETS = ('x-tag-revenue.js', 'x-tag-publish-ui.js')


def before_hash(relative, commit):
    result = subprocess.run(['git','-C',str(SOURCE),'show',commit+':'+relative], capture_output=True)
    return sha(result.stdout) if result.returncode == 0 else None


def check_base(path, relative, commits=(BASE,)):
    if file_sha(path) not in {before_hash(relative, c) for c in commits}:
        raise RuntimeError('affected_file_changed:' + str(path))


def files(root):
    return {str(p.relative_to(root)): file_sha(p) for p in root.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc' and p.name != 'tag-runtime-manifest.json'}


def validate(data):
    def nodes(rows, cents):
        if sum(r['revenue_cents'] for r in rows) != cents: raise RuntimeError('income_not_conserved')
        shares = [r.get('share_bp') for r in rows]
        if shares and all(s is not None for s in shares) and sum(shares) != 10000: raise RuntimeError('share_not_conserved')
        for row in rows:
            if 'children' in row: nodes(row['children'], row['revenue_cents'])
            elif sum(d['allocated_cents'] for d in row['dramas']) != row['revenue_cents']: raise RuntimeError('drama_income_not_conserved')
    for profile in data['accounts'].values():
        nodes(profile.get('audiences', profile.get('types', [])), profile['revenue_cents'])


def publish_data(data):
    data['meta'] = dict(data['meta'], version=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'),
                        generated_at=datetime.now(timezone.utc).isoformat(), projection_only=True)
    validate(data)
    env = environment(MAIN / '.env')
    with closing(ProfileWriter(env)) as writer: proof = writer.publish(data, Reader(env))
    serving = publish(ROOT, data, proof)
    old = json.loads((ROOT/'current.json').read_text())
    filename = 'snapshots/' + data['meta']['version'] + '.json'
    atomic_json(ROOT/filename, data)
    atomic_json(ROOT/'current.json', dict(file=filename, version=data['meta']['version'], previous=old['file']))
    atomic_json(ROOT/'status.json', dict(status='ok', attempted_at=datetime.now(timezone.utc).isoformat()))
    return dict(database=proof, serving=serving)


def restore(info):
    if str(CURRENT.resolve()) not in (info['previous'], info['release']): raise RuntimeError('later_runtime_change')
    for name, row in info['files'].items():
        if file_sha(Path(name)) not in (row['before'], row['after']): raise RuntimeError('later_file_change:' + name)
    # Derived portraits are rebuilt with a new version. Never restore publishing DBs.
    publish_data(json.loads((Path(info['backup'])/'portrait-before.json').read_text()))
    for name, row in info['files'].items(): atomic(Path(name), Path(row['backup']).read_bytes())
    switch_current(info['previous'])
    reload_services()


def install(wait_seconds):
    mount_check(); commit = source_commit()
    previous = CURRENT.resolve(strict=True)
    check_base(previous/POLICY, POLICY)
    for relative in MODULES:
        check_base(MAIN/relative, relative)
        check_base(previous/relative, relative, (BASE, OLD_RUNTIME))
    release = Path('/mnt/data-disk/x-tag-publisher/releases') / (commit+'-audience-theme')
    if release.exists(): raise RuntimeError('candidate_release_exists')
    before = files(previous)
    shutil.copytree(previous, release, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for relative in [*MODULES, POLICY]: shutil.copy2(SOURCE/relative, release/relative)
    after = files(release)
    changed = {name for name in set(before)|set(after) if before.get(name)!=after.get(name)}
    if not changed <= set(MODULES+[POLICY]): raise RuntimeError('unexpected_runtime_scope')
    # Regenerate the manifest from the CURRENT runtime, retaining pipeline additions.
    save(release/'tag-runtime-manifest.json', dict(source=commit, previous=str(previous), files=after))
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    env = dict(os.environ, X_TAG_TEST_RUNTIME=str(release))
    test = subprocess.run([sys.executable,'-B',str(SOURCE/'scripts/test_x_tag_publish.py'),'-q'], cwd=release, env=env, capture_output=True, text=True)
    (backup/'selection-tests.txt').write_text(test.stdout+test.stderr)
    if test.returncode: raise RuntimeError('runtime_selection_tests_failed')
    native = subprocess.run([sys.executable,'-B','-m','unittest','scripts.test_x_tag_dispatch.NativeTests','-q'],cwd=release, capture_output=True,text=True)
    (backup/'pipeline-tests.txt').write_text(native.stdout+native.stderr)
    if native.returncode: raise RuntimeError('existing_pipeline_tests_failed')
    targets = {MAIN/relative:(SOURCE/relative).read_bytes() for relative in MODULES}
    for root in (MAIN/'static', WEB):
        for name in ASSETS:
            check_base(root/name, 'static/'+name)
            targets[root/name] = (SOURCE/'static'/name).read_bytes()
        for path in [root/'x-account-list.html', *root.glob('x-tag-*.html')]:
            old = path.read_text(); text = patch_two_levels(old) if path.name=='x-account-list.html' else old
            text = (text.replace('类型 → 受众 → 题材优先', '受众 → 题材优先')
                    .replace('按剧类型、受众、题材三级收入画像选剧', '按受众、题材两级收入画像选剧')
                    .replace('按剧类型、受众、题材逐层分配素材', '按受众、题材逐层分配素材'))
            for asset in ASSETS:
                text = re.sub(r'(/'+re.escape(asset)+r')(?:\?[^"\']*)?', r'\1?v='+sha((SOURCE/'static'/asset).read_bytes())[:12], text)
            if text != old: targets[path]=text.encode()
    info = dict(status='prepared',commit=commit,previous=str(previous),release=str(release),backup=str(backup),
                files={},runtime_changes=sorted(changed),runtime_source_hashes=before,parents_before=parents())
    for i,(path,content) in enumerate(targets.items()):
        saved=backup/str(i);shutil.copy2(path,saved)
        info['files'][str(path)]=dict(before=file_sha(path),after=sha(content),backup=str(saved))
    save(backup/'deployment.json',info)
    print(json.dumps(dict(status='prepared',backup=str(backup),selection_tests=37,pipeline_tests=2)),flush=True)
    with switch_window(backup,wait_seconds):
        if CURRENT.resolve()!=previous or files(previous)!=before: raise RuntimeError('runtime_changed_during_staging')
        info['state_before']=state()
        pointer=json.loads((ROOT/'current.json').read_text())
        data=json.loads((ROOT/pointer['file']).read_text())
        save(backup/'portrait-before.json',data)
        expected=sum(p['revenue_cents'] for p in data['accounts'].values())
        data['accounts']={uid:rebuild_profile(p) for uid,p in data['accounts'].items()}
        data['meta'].update(hierarchy_version=VERSION,allocation='audience_distinct_broad_themes')
        for name,row in info['files'].items():
            if file_sha(Path(name))!=row['before']: raise RuntimeError('file_changed_during_staging:'+name)
        try:
            info['profile']=publish_data(data)
            if info['profile']['database']['revenue']!=expected: raise RuntimeError('revenue_changed')
            for path,content in targets.items(): atomic(path,content)
            switch_current(release)
            reload_services()
            actual,meta=Store(ROOT).load(view='summary')
            if meta['hierarchy_version']!=VERSION or sum(p['revenue_cents'] for p in actual['accounts'].values())!=expected:
                raise RuntimeError('active_profile_mismatch')
            info.update(status='active',parents_after=parents(),state_after=state(),activated_at=datetime.now(timezone.utc).isoformat())
        except Exception:
            restore(info);info['status']='reverted_code_and_portrait'
            raise
        finally: save(backup/'deployment.json',info)
    print(json.dumps({k:info[k] for k in ('status','commit','backup','release')}),flush=True)


def rollback(backup,wait_seconds):
    mount_check();path=Path(backup).resolve()
    if path.parent!=BACKUPS.resolve(): raise RuntimeError('invalid_backup')
    info=json.loads((path/'deployment.json').read_text())
    with switch_window(path,wait_seconds): restore(info)
    save(path/'rollback.json',dict(status='code_and_derived_profile_only',publishing_records_preserved=True))
    print(json.dumps(dict(status='rolled_back',backup=str(path))))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('install','rollback'))
    parser.add_argument('--backup');parser.add_argument('--wait-seconds',type=int,default=1200)
    args=parser.parse_args()
    install(args.wait_seconds) if args.action=='install' else rollback(args.backup,args.wait_seconds)
