"""Apply the verified indexed YouTube queries to the existing scoped release."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('sha')
    parser.add_argument('--deployment', required=True, type=Path)
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[2]
    d = deploy.module(release / 'ops/moboreels-attribution-20261113/deploy.py', 'original_deploy')
    lanes = deploy.module(release / deploy.OPS / 'lanes.py', 'scoped_lanes')
    d.verify(release, args.sha)
    directory = args.deployment.resolve()
    d.require(directory.parent == d.DATA / 'deployments' and directory.name.startswith('post-products-'), 'Unexpected deployment')
    state = json.loads((directory / 'manifest.json').read_text())
    d.require(state['commit'] == 'e5b1ee06c07def5262b2eb2888d6cf94f784b8b3', 'Unexpected parent release')
    receipt_path = directory / 'private/youtube-indexed-refinement.json'
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        d.require(receipt['commit'] == args.sha, 'Refinement belongs to another commit')
    else:
        d.require(state['groups']['youtube']['status'] == 'activated' and state['groups']['youtube']['resumed'], 'Parent release not active')
        deploy.current_fences(d, state, 'youtube', installed=True)
        rows = []
        for relative, target, source in (
            ('features/youtube_auto_publish/source.py', '/root/drama_material_service/features/youtube_auto_publish/source.py', 'runtime/main/features/youtube_auto_publish/source.py'),
            ('youtube-material-source.sql', '/etc/youtube-auto-publish/material-source.sql', 'youtube-material-source.sql'),
            ('youtube-material-source.uploaders.sql', '/etc/youtube-auto-publish/material-source.uploaders.sql', 'youtube-material-source.uploaders.sql'),
        ):
            original = next((r for r in state['files'] if r['target'] == target), None)
            before = original['after'] if original else None
            entry = {'lane': 'main', 'phase': 'main', 'relative': relative, 'target': target,
                     'source': str(release / deploy.OPS / source), 'before': before,
                     'after': d.digest(release / deploy.OPS / source), 'mode': 0o644}
            d.fence(entry)
            if target.endswith('.py'):
                compile(Path(entry['source']).read_bytes(), target, 'exec')
            if before:
                backup = directory / 'private' / ('indexed-before-' + Path(target).name)
                shutil.copy2(target, backup); os.chmod(backup, 0o600)
                d.require(d.digest(backup) == before, 'Refinement backup changed')
                entry['backup'] = str(backup)
            rows.append(entry)
        baseline = directory / 'private/youtube-indexed-scheduler-before.json'
        d.run(sys.executable, d.HEALTH, 'snapshot', '--lane', 'youtube', '--output', baseline)
        config = deploy.config()
        receipt = {'commit': args.sha, 'release': str(release), 'files': rows, 'status': 'draining',
                   'baseline': str(baseline), 'before': config, 'paused_by_us': 'youtube' in config['enabled_channels']}
        d.save(receipt_path, receipt)
        if receipt['paused_by_us']:
            lanes.set_lane_scoped('youtube', False, config['version'], True, directory / 'private/youtube-indexed-pause.json')
    if receipt['status'] in ('draining', 'installing'):
        d.busy(state['specs']['youtube'], directory, 'youtube-indexed')
        for row in receipt['files']:
            actual = d.digest(row['target']) if Path(row['target']).is_file() else None
            d.require(actual in (row['before'], row['after']), 'Unrelated live query change')
            d.require(d.digest(row['source']) == row['after'], 'Refinement source changed')
        receipt['status'] = 'installing'; d.save(receipt_path, receipt)
        for row in receipt['files']:
            d.atomic_copy(row['source'], row['target'], row['mode'])
        # Keep the original pre-feature backups for a complete code-only rollback.
        for row in receipt['files']:
            original = next((r for r in state['files'] if r['target'] == row['target']), None)
            if original:
                original.update(source=row['source'], after=row['after'])
            else:
                state['files'].append(dict(row))
        state['youtube_refinement'] = {'commit': args.sha, 'release': str(release), 'receipt': str(receipt_path)}
        d.save(directory / 'manifest.json', state)
        for unit in ('drama-material-api.service', 'youtube-auto-publish-worker.service'):
            d.run('systemctl', 'restart', unit); d.run('systemctl', 'is-active', unit)
        deploy.current_fences(d, state, 'youtube', installed=True)
        receipt['status'] = 'installed'; d.save(receipt_path, receipt)
    if receipt['status'] == 'installed':
        d.run(sys.executable, d.HEALTH, 'restore', '--baseline', receipt['baseline'], '--wait-seconds', '25',
              '--output', directory / 'private/youtube-indexed-scheduler-after.json')
        if receipt['paused_by_us']:
            lanes.reopen_original_lane(directory / 'private/youtube-indexed-pause.json', directory / 'private/youtube-indexed-resume.json')
        receipt['status'] = 'complete'; d.save(receipt_path, receipt)
    print(json.dumps({'status': receipt['status'], 'files': len(receipt['files']), 'commit': args.sha}))


if __name__ == '__main__':
    main()
