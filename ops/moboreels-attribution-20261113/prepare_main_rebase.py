"""Stage reviewed merges against newly captured live files; never change live code."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import time

OPS = 'ops/moboreels-attribution-20261113'
BASELINE = OPS + '/main-live-20261113'
NAME = 'main-live-rebase'
BUSINESS_SHA = 'b53d3ac4c34b9e5c5164b7e56696f13e9a59de09'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blob(release, sha, relative):
    return hashlib.sha256(subprocess.check_output(['git','-C',str(release),'show',sha+':'+relative])).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('sha')
    parser.add_argument('--deployment', type=Path, required=True)
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[2]
    deploy = load('main_rebase_deploy', release/OPS/'deploy.py')
    durable = load('main_rebase_durable', release/OPS/'report_query_fix.py').durable_json
    deploy.verify(release, args.sha)
    d = args.deployment.resolve()
    deploy.require(d.parent == (deploy.DATA/'deployments').resolve(), 'Unexpected deployment')
    path = d/'manifest.json'
    original_bytes = path.read_bytes()
    manifest = json.loads(original_bytes)
    deploy.require(manifest['commit'] == BUSINESS_SHA, 'Original business version differs')
    deploy.verify(Path(manifest['release']), BUSINESS_SHA)
    deploy.require('main' not in manifest['phases'], 'Main deployment already started')
    deploy.require(NAME not in manifest.get('prepared_hotfixes', {}), 'Rebase already prepared')
    deploy.require(not any(p.get('status')=='installing' for p in manifest['phases'].values()), 'Wait for current activation')
    directory = d/'private'/(NAME+'-'+str(time.time_ns()))
    directory.mkdir(mode=0o700)
    mappings = [('app.py', deploy.MAIN/'app.py', 'app.py'),
                ('static/quick-nav.js', deploy.MAIN/'static/quick-nav.js', 'quick-nav.js'),
                ('static/quick-nav.js', deploy.PUBLIC/'quick-nav.js', 'quick-nav.js'),
                ('static/fb-auto-publish-template.html', deploy.MAIN/'static/fb-auto-publish-template.html', 'fb-auto-publish-template.html'),
                ('static/fb-auto-publish-template.html', deploy.PUBLIC/'fb-auto-publish-template.html', 'fb-auto-publish-template.html'),
                ('features/fb_auto_posts/core.py', deploy.MAIN/'features/fb_auto_posts/core.py', None),
                ('features/fb_auto_posts/service.py', deploy.MAIN/'features/fb_auto_posts/service.py', None)]
    overrides = []
    for number,(relative,target,baseline_name) in enumerate(mappings):
        source = release/relative
        after = blob(release,args.sha,relative)
        deploy.require(deploy.digest(source)==after, 'Git source differs')
        rows = [r for r in manifest['files'] if r['target']==str(target)]
        deploy.require(len(rows)==1 and rows[0]['phase']=='main', 'Missing main planned entry')
        old = copy.deepcopy(rows[0])
        deploy.require(deploy.digest(old['staged'])==old['after'] and not Path(old['backup']).exists(), 'Original stage or backup differs')
        deploy.require(old['after']==blob(Path(manifest['release']), BUSINESS_SHA, relative), 'Original staged source differs')
        baseline = release/BASELINE/baseline_name if baseline_name else None
        baseline_relative = BASELINE+'/'+baseline_name if baseline_name else None
        if baseline_name:
            mode = 'captured_git'
            before = blob(release,args.sha,baseline_relative)
            deploy.require(deploy.digest(baseline)==before, 'Captured Git baseline differs')
            deploy.require(target.is_file() and deploy.digest(target)==before, 'Live baseline changed again: '+str(target))
        else:
            mode = 'original_manifest'
            before = old['before']
            deploy.fence(old)
        if relative.endswith('.py'):
            compile(source.read_bytes(),relative,'exec')
        staged = d/'staged'/(directory.name+'-'+str(number))
        deploy.atomic_copy(source,staged,0o600)
        deploy.require(deploy.digest(staged)==after, 'Merged staged artifact differs')
        replacement = dict(old,source=str(source),staged=str(staged),before=before,after=after)
        overrides.append({'target':str(target),'baseline_mode':mode,
                          'baseline_source':str(baseline) if baseline is not None else None,'baseline_relative':baseline_relative,
                          'source_relative':relative,'original':old,'replacement':replacement})
        rows[0].clear();rows[0].update(replacement)
    record = {'name':NAME,'status':'prepared','commit':args.sha,'release':str(release),'business_sha':BUSINESS_SHA,
              'created_at':deploy.utc(),'receipt':str(directory/'receipt.json'),'main_overrides':overrides,'live_changed':False}
    deploy.require(path.read_bytes()==original_bytes, 'Deployment changed during staging')
    for item in overrides:
        deploy.fence(item['replacement'])
    durable(record['receipt'],record)
    manifest.setdefault('prepared_hotfixes',{})[NAME] = record
    durable(path,manifest)
    print(json.dumps({'status':'prepared','commit':args.sha,'files':len(overrides),'live_changed':False,'receipt':record['receipt']}))


if __name__ == '__main__':
    main()
