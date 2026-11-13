"""Install Page-caption precedence without rewriting frozen publications."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release
from scripts.deploy_pipeline_runtime import verify_source

BASE = '7de3b074216efacb1c7f001932facd430b82712d'
FILES = ['features/fb_auto_posts/' + name + '.py' for name in
         ('core', 'hit_material', 'page_caption')]
TESTS = ['tests.test_fb_page_caption', 'tests.test_fb_profile_integration', 'tests.test_fb_profile_selection',
         'scripts.test_fb_hit_drama', 'scripts.test_fb_target_midnight', 'scripts.test_fb_auto_validation']
OVERLAY_BASES = {
    'features/fb_auto_posts/core.py': '477a884c180cf0b45431f4dc430816777d03e3698ee960ee848e691df11ec0a2',
}


def compose(baseline, changed, current):
    """Apply exact source edits to the separately verified concurrent overlay."""
    import difflib
    old, new = baseline.splitlines(keepends=True), changed.splitlines(keepends=True)
    result = current
    for kind, i1, i2, j1, j2 in reversed(difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()):
        if kind=='equal':continue
        for size in (3,2,1):
            context=''.join(old[max(0,i1-size):i1])
            anchor=context+''.join(old[i1:i2])
            if anchor and result.count(anchor)==1:break
        else:raise AssertionError('overlay_edit_anchor_changed')
        result=result.replace(anchor,context+''.join(new[j1:j2]),1)
    return result


def verify(candidate, folder):
    """Read and render the approved captions as the actual FB service user."""
    code = '''import importlib,json,pathlib,sqlite3
from contextlib import closing
for name in ('core','repositories','hit_material','page_caption'):
 importlib.import_module('features.fb_auto_posts.'+name)
from types import SimpleNamespace
from features.fb_auto_posts import page_caption
from features.fb_auto_posts.core import FBAutoPostStore
dbpath=pathlib.Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
with closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True)) as db:
 ids=[row[0] for row in db.execute("SELECT page_id FROM fb_video_page WHERE tenant_key='149bb4a14b57975f' AND created_by='892fd2e8' AND deleted=0")]
 snapshot=page_caption.load(db,'892fd2e8',ids)
assert len(ids)==236 and len(snapshot['pages'])==236,'managed_scope_changed'
material=SimpleNamespace(drama_name='Example',material_name='Clip',content_id='123',drama_description='Example story')
for pid in ids:
 selected,audit=page_caption.choose({'message_template':'Fallback'},pid,material,snapshot)
 assert audit['source']=='page_management','page_caption_unavailable:'+pid
 text=FBAutoPostStore._message(selected,material,'https://example.invalid/view',strict_length=True)
 assert text.count('https://example.invalid/view')==1 and 'Example story' in text
 assert '{url}' not in text and '{desc}' not in text
print(json.dumps(dict(pages_read=len(ids),page_caption_selected=len(ids),placeholder_render_verified=True)))'''
    unit = 'fb-auto-post-service.service'
    user = release.run('systemctl', 'show', unit, '-p', 'User', '--value') or 'root'
    proof = subprocess.run(['runuser', '-u', user, '--', sys.executable, '-B', '-c', code],
                           cwd=candidate, capture_output=True, text=True, timeout=30)
    if proof.returncode:
        raise RuntimeError('service_user_caption_check_failed:' + proof.stderr[-1500:])
    result = json.loads(proof.stdout)
    release.save(folder / 'caption-read-check.json', result)
    return result


def prepare(folder, tests):
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 3 * 1024**3, 'insufficient_data_disk_space'
    commit = verify_source(SOURCE, 'HEAD')
    origin = release.run('git', '-C', SOURCE, 'remote', 'get-url', 'origin').rstrip('/')
    assert origin in ('https://github.com/gaoyuan12356416/ai-', 'https://github.com/gaoyuan12356416/ai-.git',
                      'git@github.com:gaoyuan12356416/ai-.git', 'ssh://git@github.com/gaoyuan12356416/ai-.git'), 'unexpected_source_repository'
    current, _, ledger = release.settings('fb')
    previous, candidate = current.resolve(), folder / 'candidate'
    assert current.is_symlink(), 'runtime_pointer_required'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    for target in [candidate, *candidate.rglob('*')]:
        original = previous / target.relative_to(candidate)
        if original.exists() and not target.is_symlink():
            stat = original.stat()
            os.chown(target, stat.st_uid, stat.st_gid)
    info = dict(platform='fb', commit=commit, baseline=BASE, source=str(SOURCE),
                previous=str(previous), candidate=str(candidate), files={}, static={}, status='prepared')
    for name in FILES:
        live, target = previous / name, candidate / name
        assert target.resolve().is_relative_to(candidate.resolve()), 'overlay_symlink_escape:' + name
        baseline = subprocess.run(['git', '-C', str(SOURCE), 'show', BASE + ':' + name], capture_output=True)
        content = (SOURCE / name).read_bytes()
        if baseline.returncode == 0:
            if live.read_bytes().replace(b'\r\n', b'\n') != baseline.stdout:
                assert release.sha(live)==OVERLAY_BASES.get(name),'baseline_changed:'+name
                content=compose(baseline.stdout.decode(),content.decode(),live.read_text()).encode()
        else:
            assert not live.exists(), 'new_file_collision:' + name
        if name.endswith('.py'):
            compile(content, name, 'exec')
        else:
            json.loads(content)
        release.atomic(target, content)
        info['files'][name] = dict(before=release.sha(live), after=release.sha(target))
    for module in tests:
        name = module.replace('.', '/') + '.py'
        assert (SOURCE / name).is_file(), 'test_module_missing:' + module
        release.atomic(candidate / name, (SOURCE / name).read_bytes())
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', *tests, '-q'],
                           cwd=candidate, capture_output=True, text=True, timeout=180)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr)
    if proof.returncode:
        # A separate audience-catalog release changed this old label fixture.
        # Accept only this exact, independently reproduced baseline failure;
        # any caption/integration error or additional failure blocks activation.
        known='tests.test_fb_profile_selection.CatalogTests.test_same_shared_product_language_and_labels_are_used'
        output=proof.stdout+proof.stderr
        assert output.count('FAIL: ')==1 and 'ERROR: ' not in output and 'FAILED (failures=1)' in output, 'candidate_tests_failed'
        assert 'FAIL: test_same_shared_product_language_and_labels_are_used (tests.test_fb_profile_selection.CatalogTests)' in output, 'candidate_tests_failed'
        baseline_check=subprocess.run([sys.executable,'-B','-m','unittest',known,'-q'],cwd=previous,capture_output=True,text=True,timeout=30)
        before_output=baseline_check.stdout+baseline_check.stderr
        (folder/'baseline-profile-fixture.txt').write_text(before_output)
        assert baseline_check.returncode and "AssertionError: '__unknown__' != 'female'" in before_output and "AssertionError: '__unknown__' != 'female'" in output,'baseline_failure_not_reproduced'
        info['preexisting_test_failures']=[known]
    info['caption_read_check'] = verify(candidate, folder)
    # Patch only the copy label/help on each current static surface.
    name='fb-auto-publish-template.html'
    source_html=(SOURCE/'static'/name).read_text()
    import re
    label=re.search(r'<label class="required" for="message">.*?</label>',source_html)[0]
    hint=re.search(r'<div class="hint">新任务优先.*?</div>',source_html)[0]
    for root in (candidate/'static', release.MAIN, release.WEB):
        path=root/name
        original=path.read_text()
        content=re.sub(r'<label class="required" for="message">.*?</label>',lambda _:label,original,count=1)
        anchor='<div class="hint">{{desc}} 为短剧描述；{{url}} 为带 AIpost 归因的短链，每份文案最多使用一次。</div>'
        assert content.count(anchor)==1 or content.count(hint)==1,'caption_help_anchor_changed:'+str(path)
        content=content.replace(anchor,hint,1).encode()
        if root==candidate/'static':
            release.atomic(path,content);continue
        index=str(len(info['static']));old=folder/('static-'+index+'.old');new=folder/('static-'+index+'.new')
        old.write_bytes(path.read_bytes());new.write_bytes(content)
        import hashlib
        info['static'][str(path)]=dict(before=release.sha(path),after=hashlib.sha256(content).hexdigest(),backup=str(old),next=str(new))
    with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as src, \
            closing(sqlite3.connect(folder / 'ledger-before.sqlite3')) as dst:
        src.backup(dst)
    (folder / 'ledger-before.sqlite3').chmod(0o600)
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, folder=str(folder), files=len(FILES),
                          caption_read_check=info['caption_read_check'])), flush=True)


def activate_stopped(folder):
    """Recover an already stopped FB API; there are no accepted handlers to drain."""
    from scripts import deploy_fb_skipped_recheck as fb
    info=json.loads((folder/'manifest.json').read_text())
    current=release.verify_current(info,False)
    unit='fb-auto-post-service.service'
    assert fb.show(unit,'MainPID')=='0','service_became_active'
    release.run(sys.executable,release.HEALTH,'snapshot','--lane','fb','--output',folder/'schedulers-before-stopped.json')
    states={name:fb.show(name) for name in fb.TIMERS};before=fb.configuration()
    try:
        fb.fb_enabled(False)
        for name,state in states.items():
            if state=='active':fb.run(['systemctl','stop',name])
        assert fb.native_active()==0,'native_work_preserved'
        assert not release.run('ss','-Hlnt','( sport = :18835 )'),'fb_listener_became_active'
        release.run('systemctl','stop',unit)
        assert fb.show(unit,'MainPID')=='0','fb_process_not_stopped'
        release.verify_current(info,False)
        for name,row in info['files'].items():assert release.sha(Path(info['candidate'])/name)==row['after'],'candidate_changed'
        temporary=current.with_name('current.caption-next')
        assert not temporary.exists() and not temporary.is_symlink()
        temporary.symlink_to(info['candidate']);temporary.replace(current)
        release.run('systemctl','start',unit)
        release.health(18835)
        release.publish_static(info)
    finally:
        fb.fb_enabled('fb' in before['enabled_channels'],'fb' in before.get('preparation_only_channels',[]))
        for name,state in states.items():
            if state=='active':fb.run(['systemctl','start',name])
        release.run(sys.executable,release.HEALTH,'restore','--baseline',folder/'schedulers-before-stopped.json','--wait-seconds','25','--output',folder/'schedulers-after.json')
    release.health(18835)
    info.update(status='active',controller_commit=release.run('git','-C',SOURCE,'rev-parse','HEAD'),recovered_stopped_api=True)
    release.save(folder/'manifest.json',info)
    print(json.dumps(dict(status='active',commit=info['commit'],recovered_stopped_api=True)),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback', 'verify'))
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--test-module', action='append')
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk'), 'data_disk_folder_required'
    if args.action == 'prepare':
        prepare(folder, args.test_module or TESTS)
    elif args.action == 'verify':
        info = json.loads((folder / 'manifest.json').read_text())
        current = release.verify_current(info, rollback=True)
        print(json.dumps(verify(current.resolve(), folder)), flush=True)
    else:
        if args.action=='apply' and release.run('systemctl','show','fb-auto-post-service.service','-p','MainPID','--value')=='0':
            activate_stopped(folder)
        else:
            release.activate('fb', folder, args.action == 'rollback', native_wait_seconds=300)
        if args.action == 'apply':
            info = json.loads((folder / 'manifest.json').read_text())
            verify(Path(info['candidate']), folder)


if __name__ == '__main__':
    main()
