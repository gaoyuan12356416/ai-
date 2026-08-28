#!/usr/bin/env python3
"""Run the unchanged cover service with CPU-owned auth and disk-backed CLI state.

The short auth-copy lock is the existing screenshot lock. It is not a
cross-machine publishing/credential lease. The source service must be stopped
before this production endpoint is started.
"""
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

from mount_guard import check_mount


def atomic_copy(source, target):
    temporary = target.with_name(target.name + '.tmp.%s' % os.getpid())
    shutil.copy2(str(source), str(temporary))
    os.replace(str(temporary), str(target))


def sync_auth(source_home, target_home, lock_path, outbound=False):
    names = ('auth.json',) if outbound else (
        'auth.json', 'config.toml', 'installation_id', 'version.json', 'models_cache.json'
    )
    with open(str(lock_path), 'a+') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            if not (source_home / 'auth.json').is_file():
                raise RuntimeError('CPU Codex authorization is missing')
            for name in names:
                source = source_home / name
                if source.is_file():
                    atomic_copy(source, target_home / name)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    home = Path(os.environ['CODEX_HOME'])
    source = Path(os.environ.get('CODEX_SCREENSHOT_SOURCE_CODEX_HOME', '/root/.codex'))
    lock = Path(os.environ.get('CODEX_SCREENSHOT_AUTH_SYNC_LOCK_PATH', '/tmp/codex_screenshot_auth_sync.lock'))
    check_mount([str(home), os.environ['CODEX_COVER_WORK_ROOT'], os.environ['CODEX_COVER_PUBLIC_ROOT']])
    if home.resolve() == source.resolve():
        raise RuntimeError('cover CLI state must be separate from the CPU interactive home')
    entry = Path(__file__).parent / 'runtime' / 'codex_cover_service.py'
    spec = importlib.util.spec_from_file_location('frozen_cover_service', str(entry))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def run_with_cpu_auth(command, timeout=None):
        sync_auth(source, home, lock)
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, timeout=timeout, env=os.environ.copy())
        finally:
            sync_auth(home, source, lock, outbound=True)
        if result.returncode:
            raise RuntimeError('Codex cover subprocess failed with exit code %s' % result.returncode)
        return result

    module.run_cmd = run_with_cpu_auth
    module.main()


if __name__ == '__main__':
    main()
