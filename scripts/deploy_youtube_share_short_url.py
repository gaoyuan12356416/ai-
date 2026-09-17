#!/usr/bin/env python3
"""Add the frozen short-link macro; restart only the main API, keep all ledgers."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import time

from deploy_youtube_share_x import API, BASE, MAIN, PUBLIC, healthy, install, run, sha

FILES = {
    'features/youtube_auto_publish/x_share.py': '55291486c62feac7eb81ccac2e096d50a4985038f9a64272e0475a283b8e94fa',
    'features/x_accounts/youtube_share_text.py': '7033ac3cf2b481b8202d65100820d68c49edcdbc8057753b96542c17ae18cdc1',
    'static/youtube-publish.js': 'a03cc8ca512b65097a9b82d47e35f326d57ab375cda14839a77303c6f3de7aea',
    'static/youtube-publish.html': 'eb2c73276d44cb230f8bfab02fede34310514ead7ecca15a415f5572953acfd1',
}
KIND = 'youtube-share-short-url'


def service_state():
    return {unit: run('systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'MainPID', '-p', 'NRestarts')
            for unit in [API, 'x-post-automation.service', 'youtube-auto-publish-worker.service']}


def restore(backup, rows):
    for row in rows:
        if sha(backup / row['saved']) != row['before']:
            raise RuntimeError('Backup integrity mismatch')
    for row in rows:
        install(backup / row['saved'], row['target'], row['mode'])


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--commit')
    action.add_argument('--rollback', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Unexpected data disk')
    os.umask(0o077)
    stage = Path(__file__).resolve().parents[1]
    inputs = [(stage / name, MAIN / name, expected) for name, expected in FILES.items()]
    inputs += [(stage / name, PUBLIC / Path(name).name, expected)
               for name, expected in FILES.items() if name.startswith('static/')]
    with (BASE / 'youtube-share-x-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            backup = args.rollback.resolve()
            if (BASE / 'backups').resolve() not in backup.parents:
                raise RuntimeError('Invalid backup path')
            manifest = json.loads((backup / 'manifest.json').read_text())
            rows = manifest['files']
            if manifest.get('kind') != KIND or {r['target'] for r in rows} != {str(t) for _, t, _ in inputs}:
                raise RuntimeError('Rollback target mismatch')
            if any(sha(r['target']) != r['after'] for r in rows):
                raise RuntimeError('Newer files exist; refusing to overwrite')
            if any(sha(backup / r['saved']) != r['before'] for r in rows):
                raise RuntimeError('Backup integrity mismatch')
            if args.check:
                print(json.dumps({'rollback_check': 'passed', 'backup': str(backup)}))
                return
            try:
                run('systemctl', 'stop', API)
                restore(backup, rows)
            finally:
                run('systemctl', 'start', API)
                healthy()
            print(json.dumps({'rollback': str(backup), 'services': service_state(), 'ledgers': 'retained'}))
            return
        if not re.fullmatch(r'[0-9a-f]{40}', args.commit or '') or (stage / '.github-verified-commit').read_text().strip() != args.commit:
            raise RuntimeError('Deploy an exact GitHub-fetched commit')
        def baseline():
            for source, target, expected in inputs:
                if not source.is_file() or sha(target) != expected:
                    raise RuntimeError('Baseline changed: ' + str(target))
        baseline()
        for name in FILES:
            path = stage / name
            if path.suffix == '.py': compile(path.read_bytes(), str(path), 'exec')
        healthy()
        before_services = service_state()
        if args.check:
            print(json.dumps({'check': 'passed', 'commit': args.commit, 'targets': len(inputs), 'services': before_services}))
            return
        backup = BASE / 'backups' / (KIND + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(parents=True, exist_ok=False)
        rows = []
        for index, (source, target, _) in enumerate(inputs):
            saved = 'file-' + str(index)
            shutil.copy2(target, backup / saved)
            rows.append({'target': str(target), 'saved': saved, 'before': sha(target), 'after': sha(source),
                         'mode': target.stat().st_mode & 0o777})
        manifest = {'kind': KIND, 'commit': args.commit, 'files': rows, 'services_before': before_services}
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        changed = False
        try:
            run('systemctl', 'stop', API)
            baseline()
            changed = True
            for (source, target, _), row in zip(inputs, rows): install(source, target, row['mode'])
            run('systemctl', 'start', API)
            healthy()
            if any(sha(r['target']) != r['after'] for r in rows):
                raise RuntimeError('Installed hash mismatch')
        except BaseException:
            try:
                if changed:
                    run('systemctl', 'stop', API)
                    restore(backup, rows)
            finally:
                run('systemctl', 'start', API)
                healthy()
            raise
        result = dict(commit=args.commit, backup=str(backup), services=service_state(), sha256={r['target']: sha(r['target']) for r in rows})
        (backup / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


if __name__ == '__main__':
    main()
