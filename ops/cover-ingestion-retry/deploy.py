"""Install only three callback/message functions and the shared image GET helper."""
import argparse
import ast
import importlib.util
import json
from pathlib import Path
import subprocess

RELEASE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('cover_deploy_base', RELEASE / 'ops/ai-cover-custom-source/deploy.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
NAMES = ('notify_screenshot_ai_source_callback', 'humanize_screenshot_failure_reason', 'build_screenshot_failure_message')
CORE = 'features/cover_material_source.py'


def compose(original, replacement, baseline):
    if base.digest(original) != baseline['app_sha256']:
        raise RuntimeError('live_app_drift')
    old, new = original.decode(), replacement.decode()
    tree, fresh = ast.parse(old), ast.parse(new)
    changes = []
    lines = old.splitlines(keepends=True)
    for name in NAMES:
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        target = next(n for n in fresh.body if isinstance(n, ast.FunctionDef) and n.name == name)
        if base.digest(ast.get_source_segment(old, node).encode()) != baseline['functions'][name]:
            raise RuntimeError('live_function_drift:' + name)
        start = sum(len(s) for s in lines[:node.lineno - 1])
        end = sum(len(s) for s in lines[:node.end_lineno - 1]) + len(lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())
        changes.append((start, end, ast.get_source_segment(new, target)))
    merged = old
    for start, end, text in sorted(changes, reverse=True):
        merged = merged[:start] + text + merged[end:]
    def other_ast(text):
        t = ast.parse(text)
        t.body = [n for n in t.body if not (isinstance(n, ast.FunctionDef) and n.name in NAMES)]
        return ast.dump(t, include_attributes=False)
    if other_ast(old) != other_ast(merged):
        raise RuntimeError('unrelated_app_ast_changed')
    compile(merged, str(base.APP), 'exec')
    return merged.encode()


def stopped():
    if base.unit_state(base.APP_UNIT) not in ('inactive', 'failed'):
        raise RuntimeError('stop_main_api_before_install_or_rollback')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--backup', required=True)
    parser.add_argument('--repo')
    parser.add_argument('--commit')
    args = parser.parse_args()
    base.environment_ok()
    backup = Path(args.backup).resolve()
    if not backup.is_relative_to(Path('/mnt/data-disk/ai-cover-material-source')):
        raise RuntimeError('backup_must_be_on_cover_data_disk')
    if args.action == 'prepare':
        if RELEASE.name != args.commit:
            raise RuntimeError('release_directory_must_match_commit')
        refs = base.output('git', '-C', args.repo, 'for-each-ref', '--format=%(refname)', '--contains', args.commit, 'refs/remotes/origin/').strip()
        if not refs:
            raise RuntimeError('commit_not_fetched_from_origin')
        records = base.output('git', '-C', args.repo, 'ls-tree', '-r', '--name-only', args.commit, '--', 'app.py', CORE, 'ops/cover-ingestion-retry', 'ops/ai-cover-custom-source/deploy.py')
        for relative in records.decode().splitlines():
            if (RELEASE / relative).read_bytes() != base.output('git', '-C', args.repo, 'show', args.commit + ':' + relative):
                raise RuntimeError('release_differs_from_github:' + relative)
        baseline = json.loads((RELEASE / 'ops/cover-ingestion-retry/baseline.json').read_text())
        original, core = base.APP.read_bytes(), base.CORE.read_bytes()
        if base.digest(core) != baseline['core_sha256']:
            raise RuntimeError('live_core_drift')
        merged = compose(original, (RELEASE / 'app.py').read_bytes(), baseline)
        compile((RELEASE / CORE).read_bytes(), CORE, 'exec')
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, data in [('app.before.py', original), ('core.before.py', core), ('app.after.py', merged),
                           ('core.after.py', (RELEASE / CORE).read_bytes())]:
            base.atomic_write(backup / name, data)
        base.save_meta(backup, {'commit': args.commit, 'phase': 'prepared', 'at': base.now(),
                              'files': {n: base.digest((backup / n).read_bytes()) for n in
                                        ('app.before.py', 'core.before.py', 'app.after.py', 'core.after.py')}})
    else:
        stopped()
        meta = json.loads((backup / 'deployment.json').read_text())
        for name, expected in meta['files'].items():
            if base.digest((backup / name).read_bytes()) != expected:
                raise RuntimeError('backup_changed:' + name)
        for path, stem in ((base.CORE, 'core'), (base.APP, 'app')):
            current = base.digest(path.read_bytes())
            permitted = {meta['files'][stem + '.before.py']}
            if args.action == 'rollback':
                permitted.add(meta['files'][stem + '.after.py'])
            if current not in permitted:
                raise RuntimeError('production_changed:' + stem)
        meta['phase'] = args.action + '_started'
        base.save_meta(backup, meta)
        suffix = '.after.py' if args.action == 'apply' else '.before.py'
        for path, stem in ((base.CORE, 'core'), (base.APP, 'app')):
            base.atomic_write(path, (backup / (stem + suffix)).read_bytes())
        meta['phase'] = 'installed' if args.action == 'apply' else 'rolled_back'
        base.save_meta(backup, meta)
    print(json.dumps({'action': args.action, 'backup': str(backup)}))


if __name__ == '__main__':
    main()
