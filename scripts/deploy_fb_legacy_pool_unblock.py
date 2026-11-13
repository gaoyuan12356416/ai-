"""Scoped repository policy overlay; preserve live services' other changes."""
import argparse
import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_publish_queue_isolation as ops
from scripts.deploy_pipeline_runtime import atomic_copy, preserve_tree_owners, verify_source
from scripts.deploy_fb_mixed_product_pools import activate, patch, stage

BASE = '20281127b6725b4820c3e2430ff9e6612bfdaec9'
FILE = 'features/fb_auto_posts/repositories.py'
CURRENT = Path('/opt/fb-auto-post/current')
MAIN = Path('/root/drama_material_service')
TESTS = ['tests.test_fb_legacy_queue_independence', 'tests.test_fb_mixed_product_pools',
         'scripts.test_fb_auto_validation',
         'tests.test_fb_page_task_isolation.PageIsolationTest.test_same_material_unknown_cannot_execute_even_from_another_batch',
         'tests.test_fb_page_task_isolation.PageIsolationTest.test_concurrent_claims_still_allow_only_one_page_write']


def legacy_method(text):
    cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == 'PagePoolRepository')
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'legacy_conflicts')
    return ''.join(text.splitlines(keepends=True)[fn.lineno - 1:fn.end_lineno])


def prepare(folder):
    commit = verify_source(SOURCE, 'HEAD')
    assert ops.run(['git', 'remote', 'get-url', 'origin'], cwd=SOURCE).rstrip('/').removesuffix('.git').endswith('gaoyuan12356416/ai-')
    assert ops.run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert CURRENT.is_symlink()
    previous, candidate = CURRENT.resolve(), folder / 'candidate'
    needed = sum(p.stat().st_size for p in previous.rglob('*') if p.is_file() and not p.is_symlink())
    assert shutil.disk_usage('/mnt/data-disk').free > needed * 2 + 16 * 1024**2
    original = subprocess.check_output(['git', 'show', BASE + ':' + FILE], cwd=SOURCE).decode()
    assert (previous / FILE).read_text(encoding='utf-8') == original, 'live_repository_changed'
    folder.mkdir(parents=True, exist_ok=False, mode=0o755)
    shutil.copytree(previous, candidate, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    preserve_tree_owners(previous, candidate)
    manifest = dict(commit=commit, source=str(SOURCE), baseline=BASE, external={},
                    main_pid=ops.pid('drama-material-api.service'), lanes={'fb': dict(
                    before_root=str(previous), candidate=str(candidate), files={}, state='prepared')})
    atomic_copy(SOURCE / FILE, candidate / FILE)
    compile((candidate / FILE).read_bytes(), FILE, 'exec')
    manifest['lanes']['fb']['files'][FILE] = dict(before=ops.sha(previous / FILE), after=ops.sha(candidate / FILE))
    main_text = (MAIN / FILE).read_text(encoding='utf-8')
    old_method = legacy_method(original)
    # The API copy has the same retired query with qualified ORDER BY columns.
    if old_method not in main_text:
        old_method = old_method.replace('ORDER BY queue_id,overlap_page_id', 'ORDER BY q.id,li.page_id')
    main_text = patch(main_text, [
        (old_method, legacy_method((SOURCE / FILE).read_text(encoding='utf-8')))])
    compile(main_text, FILE, 'exec')
    stage(folder, MAIN / FILE, main_text, manifest)
    for name in ('test_fb_legacy_queue_independence.py', 'test_fb_mixed_product_pools.py',
                 'test_fb_page_task_isolation.py'):
        atomic_copy(SOURCE / 'tests' / name, candidate / 'tests' / name)
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', *TESTS, '-q'],
                           cwd=candidate, capture_output=True, text=True, timeout=60)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr, encoding='utf-8')
    assert proof.returncode == 0, 'candidate_tests_failed'
    user = ops.run(['systemctl', 'show', 'fb-auto-post-service.service', '-p', 'User', '--value'])
    ops.run(['runuser', '-u', user, '--', 'test', '-r', candidate / FILE])
    manifest['preserved_files'] = {}
    for name in ('service.py', 'core.py', 'metric_mysql.py'):
        relative = 'features/fb_auto_posts/' + name
        assert ops.sha(previous / relative) == ops.sha(candidate / relative)
        manifest['preserved_files'][relative] = ops.sha(candidate / relative)
    ops.save(folder / 'manifest.json', manifest)
    print(json.dumps(dict(prepared=True, commit=commit, tests=16, folder=str(folder))), flush=True)


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
