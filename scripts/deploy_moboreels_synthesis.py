"""Install only the two reviewed API functions and MoboReels source adapter."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path('/root/drama_material_service')
SOURCE = Path(__file__).resolve().parents[1]
DATA = Path('/mnt/data-disk/moboreels-synthesis')
FUNCTIONS = ('list_products', 'fetch_drama_episodes')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def functions(source):
    return {n.name: n for n in ast.parse(source).body
            if isinstance(n, ast.FunctionDef) and n.name in FUNCTIONS}


def atomic_write(path, content, mode=0o644):
    temporary = path.with_name(path.name + '.moboreels-new')
    temporary.write_bytes(content)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def install():
    # Compare against this commit's parent functions, preserving every unrelated
    # live edit. No replacement of the old branch's full app.py is permitted.
    before = subprocess.check_output(['git', 'show', 'HEAD^:app.py'], cwd=SOURCE).decode('utf8')
    desired = (SOURCE / 'app.py').read_text(encoding='utf8')
    target = ROOT / 'app.py'
    current_bytes = target.read_bytes()
    current = current_bytes.decode('utf8')
    current_functions, before_functions, desired_functions = map(functions, (current, before, desired))
    for name in FUNCTIONS:
        if ast.dump(current_functions[name]) != ast.dump(before_functions[name]):
            raise RuntimeError('Live function changed; reconcile before install: ' + name)
        current = current.replace(ast.get_source_segment(current_bytes.decode('utf8'), current_functions[name]),
                                  ast.get_source_segment(desired, desired_functions[name]), 1)
    compile(current, str(target), 'exec')
    module_rel = 'features/moboreels_library/synthesis.py'
    module = (SOURCE / module_rel).read_bytes()
    compile(module, module_rel, 'exec')
    if (ROOT / module_rel).exists():
        raise RuntimeError('Adapter already exists; reconcile installed release')
    directory = DATA / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory.mkdir(parents=True, mode=0o700)
    files = []
    for relative, content in [('app.py', current.encode('utf8')), (module_rel, module)]:
        path = ROOT / relative
        backup = directory / Path(relative).name
        old = digest(path)
        if path.exists():
            shutil.copy2(path, backup)
        files.append({'path': str(path), 'before': old, 'after': hashlib.sha256(content).hexdigest(),
                      'backup': str(backup), 'mode': path.stat().st_mode & 0o777 if path.exists() else 0o644})
    manifest = {'commit': subprocess.check_output(['git','rev-parse','HEAD'], cwd=SOURCE, text=True).strip(),
                'files': files}
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if target.read_bytes() != current_bytes:
        raise RuntimeError('Live app changed during staging; nothing installed')
    # Install import dependency before the API starts referencing it.
    atomic_write(ROOT / module_rel, module)
    atomic_write(target, current.encode('utf8'), files[0]['mode'])
    print(json.dumps({'deployment': str(directory), 'commit': manifest['commit'], 'installed': True}))


def rollback(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    for row in manifest['files']:
        if digest(Path(row['path'])) != row['after']:
            raise RuntimeError('Later code change found; reconcile before rollback: ' + row['path'])
    for row in manifest['files']:
        path = Path(row['path'])
        if row['before'] is None:
            path.unlink()
        else:
            backup = Path(row['backup'])
            assert digest(backup) == row['before']
            atomic_write(path, backup.read_bytes(), row['mode'])
    print(json.dumps({'rolled_back': True, 'deployment': str(directory)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'rollback'))
    parser.add_argument('--deployment', type=Path)
    args = parser.parse_args()
    mounted = subprocess.check_output(['findmnt','-n','-o','UUID','/mnt/data-disk'], text=True).strip()
    assert mounted == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    if args.action == 'install':
        install()
    else:
        if not args.deployment or args.deployment.resolve().parent != DATA:
            raise ValueError('Use the exact deployment directory under ' + str(DATA))
        rollback(args.deployment)
