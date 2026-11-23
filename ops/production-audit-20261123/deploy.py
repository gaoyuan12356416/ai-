"""Apply GitHub-reviewed narrow patches while preserving each runtime's overlays.

Code only: never copies/restores databases, receipts, credentials or unit files.
Activation uses the existing service-specific graceful restart separately.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import tempfile
import time

BUNDLE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace(source, target):
    fd, name = tempfile.mkstemp(prefix='.audit-fix-', dir=str(target.parent))
    os.close(fd)
    temp = Path(name)
    try:
        shutil.copy2(str(source), str(temp))
        os.replace(str(temp), str(target))
    finally:
        if temp.exists():
            temp.unlink()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['stage', 'apply', 'rollback'])
    p.add_argument('--lane', required=True)
    p.add_argument('--state', required=True)
    a = p.parse_args()
    spec = json.loads((BUNDLE / 'manifest.json').read_text())[a.lane]
    root = Path(spec['root']).resolve()
    state = Path(a.state).resolve() / a.lane
    staged, backup = state / 'staged', state / 'backup'
    if a.action == 'stage':
        if state.exists():
            raise RuntimeError('deployment_state_already_exists')
        for item in spec['files']:
            source, target = root / item['path'], staged / item['path']
            if sha(source) != item['before']:
                raise RuntimeError('runtime_changed:' + item['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(target))
            subprocess.run(['git', 'apply', '--unsafe-paths', str(BUNDLE / 'patches' / item['patch'])], cwd=str(staged), check=True)
            if sha(target) != item['after']:
                raise RuntimeError('staged_digest_mismatch:' + item['path'])
            py_compile.compile(str(target), doraise=True)
        (state / 'state.json').write_text(json.dumps({'root': str(root), 'spec': spec, 'staged_at': time.time()}, indent=2))
    else:
        recorded = json.loads((state / 'state.json').read_text())
        if recorded['root'] != str(root) or recorded['spec'] != spec:
            raise RuntimeError('deployment_identity_changed')
        for item in spec['files']:
            target = root / item['path']
            expected = item['before'] if a.action == 'apply' else item['after']
            if sha(target) != expected:
                raise RuntimeError('runtime_changed:' + item['path'])
        for item in spec['files']:
            target, saved = root / item['path'], backup / item['path']
            if a.action == 'apply':
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(target), str(saved))
                replace(staged / item['path'], target)
            else:
                if sha(saved) != item['before']:
                    raise RuntimeError('backup_digest_mismatch')
                replace(saved, target)
        (state / (a.action + '.json')).write_text(json.dumps({'at': time.time(), 'files': spec['files']}, indent=2))
    print(json.dumps({'lane': a.lane, 'action': a.action, 'files': len(spec['files']), 'root': str(root)}))


if __name__ == '__main__':
    main()
