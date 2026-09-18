#!/usr/bin/env python3
"""Inspect (default) or apply the scoped 2026-09-18 TT Minis code rollback.

Preserve current business data, retired-snapshot receipts and unrelated cron jobs.
Renew the mtime grace of all reachable partitions before restoring the old
mtime-only publisher, so rolling back cannot break already opened report pages.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import tt_minis_storage as storage


@contextmanager
def report_lock():
    import fcntl
    with open('/tmp/tt_minis_multi_dim_dashboard.lock', 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def inspect(release):
    deployment = json.loads((release / 'deployment.json').read_text())
    live = Path('/root/codex_test')
    for name, expected in deployment['files'].items():
        if hashlib.sha256((live / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Live source changed since this deployment: ' + name)
        if not (release / 'backup' / name).is_file():
            raise RuntimeError('Missing source backup: ' + name)
    web = Path('/mnt/data-disk/tt-minis-native-growth/published')
    manifests = [web / 'latest.json']
    cutoff = time.time() - 86400
    manifests.extend(p for p in (web / 'manifest-history').glob('*.json') if p.stat().st_mtime > cutoff)
    reachable = set()
    for path in manifests:
        manifest = json.loads(path.read_text())
        for files in manifest['data_files'].values():
            for item in files.values():
                detail = (web / item['path']).resolve()
                if (web / 'data').resolve() not in detail.parents or not detail.is_file():
                    raise RuntimeError('Missing or unsafe reachable partition: ' + item['path'])
                reachable.add(detail)
    return live, reachable


def apply(release):
    with report_lock(), storage.lifecycle_lock():
        live, reachable = inspect(release)
        now = time.time()
        for path in reachable:
            os.utime(str(path), (path.stat().st_atime, now))
        for name in ('tt_minis_storage.py', 'tt_minis_multi_dim_dashboard.py'):
            backup = release / 'backup' / name
            temp = live / (name + '.rollback.tmp')
            temp.write_bytes(backup.read_bytes())
            os.chmod(str(temp), backup.stat().st_mode & 0o777)
            os.replace(str(temp), str(live / name))
        # Restore rules only when nobody has edited them since this release.
        guide = release / 'repo/ops/tt-minis-native-growth/TT_MINIS_STORAGE_AGENTS.md'
        rules_restored = (live / 'AGENTS.md').read_text().strip() == guide.read_text().strip()
        if rules_restored:
            temp = live / 'AGENTS.md.rollback.tmp'
            temp.write_bytes((release / 'backup/AGENTS.md').read_bytes())
            os.replace(str(temp), str(live / 'AGENTS.md'))
        added = set((release / 'cron-added.txt').read_text().splitlines())
        cron = subprocess.check_output(['crontab', '-l']).decode()
        updated = '\n'.join(line for line in cron.splitlines() if line not in added) + '\n'
        subprocess.run(['crontab', '-'], input=updated.encode(), check=True)
        result = {'applied': True, 'renewed_partition_grace': len(reachable),
                  'workspace_rules_restored': rules_restored, 'unrelated_cron_preserved': True,
                  'snapshot_data_changed': False, 'business_manifest_changed': False}
        (release / 'rollback-receipt.json').write_text(json.dumps(result, indent=2))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    release = storage.prepare_storage(Path(args.release))
    if storage.STORAGE.resolve() not in release.parents:
        raise RuntimeError('Release must be on the TT Minis data disk')
    if args.apply:
        result = apply(release)
    else:
        _, reachable = inspect(release)
        result = {'applied': False, 'valid_backup': True, 'reachable_partitions': len(reachable)}
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
