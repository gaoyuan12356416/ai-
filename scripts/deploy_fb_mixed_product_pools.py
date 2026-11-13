"""Deploy only mixed-product Page-pool validation and its editor text."""
import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_publish_queue_isolation as ops
from scripts.deploy_pipeline_runtime import atomic_copy, preserve_tree_owners, verify_source

BASE = 'f10d1cbd666809a8a373bdf918d8b9642d538539'
SERVICE = 'features/fb_auto_posts/service.py'
EDITOR = 'static/fb-auto-publish-template.html'
CURRENT = Path('/opt/fb-auto-post/current')
MAIN = Path('/root/drama_material_service')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
HTML_EDITS = (
    ('同一模板只能选择同一目标产品的 Page 池，素材产品可单独选择。',
     '同一模板可混选不同产品的 Page 池，素材产品可单独选择。'),
    ('Dramawave Page 池固定映射到 data_source 6。',
     '固定使用素材来源 6，按上方配置的素材产品筛选。'),
)


def method(text):
    tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Runtime')
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'resolve_source')
    return ''.join(text.splitlines(keepends=True)[node.lineno - 1:node.end_lineno])


def patch(text, edits):
    for before, after in edits:
        assert text.count(before) == 1, 'affected_text_changed'
        text = text.replace(before, after, 1)
    return text


def stage(folder, path, content, manifest):
    index = len(manifest['external'])
    old, new = folder / ('external-%s.old' % index), folder / ('external-%s.new' % index)
    shutil.copy2(path, old)
    new.write_text(content, encoding='utf-8')
    manifest['external'][str(path)] = dict(before=ops.sha(path), after=ops.sha(new),
                                         backup=str(old), next=str(new))


def prepare(folder):
    commit = verify_source(SOURCE, 'HEAD')
    assert ops.run(['git', 'remote', 'get-url', 'origin'], cwd=SOURCE).rstrip('/').removesuffix('.git').endswith('gaoyuan12356416/ai-')
    assert ops.run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert CURRENT.is_symlink()
    previous, candidate = CURRENT.resolve(), folder / 'candidate'
    needed = sum(p.stat().st_size for p in previous.rglob('*') if p.is_file() and not p.is_symlink())
    assert shutil.disk_usage('/mnt/data-disk').free > needed * 2 + 16 * 1024**2
    original = subprocess.check_output(['git', 'show', BASE + ':' + SERVICE], cwd=SOURCE).decode()
    assert (previous / SERVICE).read_text(encoding='utf-8') == original, 'live_service_baseline_changed'
    before_method, after_method = method(original), method((SOURCE / SERVICE).read_text(encoding='utf-8'))
    folder.mkdir(parents=True, exist_ok=False, mode=0o755)
    shutil.copytree(previous, candidate, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    preserve_tree_owners(previous, candidate)
    manifest = dict(commit=commit, source=str(SOURCE), baseline=BASE, external={},
                    main_pid=ops.pid('drama-material-api.service'), lanes={'fb': dict(
                    before_root=str(previous), candidate=str(candidate), files={}, state='prepared')})
    atomic_copy(SOURCE / SERVICE, candidate / SERVICE)
    staged_html = folder / 'editor.next'
    staged_html.write_text(patch((previous / EDITOR).read_text(encoding='utf-8'), HTML_EDITS), encoding='utf-8')
    atomic_copy(staged_html, candidate / EDITOR)
    for relative in (SERVICE, EDITOR):
        manifest['lanes']['fb']['files'][relative] = dict(before=ops.sha(previous / relative), after=ops.sha(candidate / relative))
    main_text = patch((MAIN / SERVICE).read_text(encoding='utf-8'), [(before_method, after_method)])
    compile(main_text, SERVICE, 'exec')
    stage(folder, MAIN / SERVICE, main_text, manifest)
    for target in (MAIN / EDITOR, Path('/usr/share/nginx/html') / Path(EDITOR).name):
        stage(folder, target, patch(target.read_text(encoding='utf-8'), HTML_EDITS), manifest)
    test = 'tests/test_fb_mixed_product_pools.py'
    atomic_copy(SOURCE / test, candidate / test)
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest',
                            'tests.test_fb_mixed_product_pools', 'scripts.test_fb_auto_validation', '-q'],
                           cwd=candidate, capture_output=True, text=True, timeout=45)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr, encoding='utf-8')
    assert proof.returncode == 0, 'candidate_tests_failed'
    user = ops.run(['systemctl', 'show', 'fb-auto-post-service.service', '-p', 'User', '--value'])
    ops.run(['runuser', '-u', user, '--', 'test', '-r', candidate / SERVICE])
    ops.save(folder / 'manifest.json', manifest)
    print(json.dumps(dict(prepared=True, commit=commit, tests=11, folder=str(folder))), flush=True)


def activate(folder, rollback=False):
    path = folder / 'manifest.json'
    manifest = json.loads(path.read_text())
    for target, item in manifest['external'].items():
        assert ops.sha(target) in {item['before'], item['after']}, 'external_source_changed:' + target
    suffix = 'rollback' if rollback else 'apply'
    baseline = folder / ('schedulers-before-' + suffix + '.json')
    ops.run([sys.executable, HEALTH, 'snapshot', '--lane', 'fb', '--output', baseline], timeout=50)
    os.environ['QUEUE_ISOLATION_DRAIN_SECONDS'] = '55'
    try:
        # Existing helper drains only the FB HTTP handlers/readback workers.
        # It neither pauses other publishing channels nor changes queue state.
        ops.switch(folder, 'fb', rollback, preserve_queue_scopes=True)
        for target, item in manifest['external'].items():
            atomic_copy(Path(item['backup' if rollback else 'next']), Path(target))
    finally:
        ops.run([sys.executable, HEALTH, 'restore', '--baseline', baseline,
                 '--wait-seconds', '25', '--output', folder / ('schedulers-after-' + suffix + '.json')], timeout=50)
    manifest = json.loads(path.read_text())
    manifest['external_state'] = 'rolled_back' if rollback else 'active'
    manifest['main_pid_after'] = ops.pid('drama-material-api.service')
    ops.save(path, manifest)
    print(json.dumps(dict(state=manifest['external_state'], main_api_restarted=False,
                          business_ledgers_preserved=True)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--folder', required=True, type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk'), 'data_disk_folder_required'
    if args.action == 'prepare':
        prepare(folder)
    else:
        activate(folder, args.action == 'rollback')
