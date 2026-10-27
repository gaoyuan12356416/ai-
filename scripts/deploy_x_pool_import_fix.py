"""Deploy the read-only source DTO fix without stopping X publishers."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time

import deploy_x_personal_publish as base

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'features/x_video_publish/sources.py'
REF = 'refs/heads/codex/x-pool-import-fix-20261027'
BASE = '7e9eeb3'
MAIN = 'drama-material-api.service'
CONTINUOUS = ['x-post-automation.service', 'x-auto-post-service.service', base.POOL,
              'drama-material-job-worker.service']


def pids():
    return {unit: base.run('systemctl', 'show', unit, '-p', 'MainPID', '--value') for unit in CONTINUOUS}


def change(config, manifest, rollback=False):
    before_pids = pids()
    before_triggers = {unit: base.unit_state(unit) for unit in base.TRIGGERS}
    token_facts = base.token_facts(config)
    manifest['continuous_service_pids_before'] = before_pids
    manifest['trigger_states_before'] = before_triggers
    base.save(manifest)
    base.snapshot(Path(config['paths']['post_db']), Path(manifest['backup']) /
                  ('before-rollback.sqlite3' if rollback else 'before.sqlite3'))
    if not rollback:
        shutil.copytree(config['paths']['tokens'], Path(manifest['backup']) / 'tokens')
        (Path(manifest['backup']) / 'tokens').chmod(0o700)
    for item in manifest['files']:
        base.require(base.digest(Path(item['target'])) == item['after' if rollback else 'before'],
                     'Live file drift; no cutover performed')
        source = Path(item['backup']) if rollback else ROOT / SOURCE
        base.require(base.digest(source) == item['before' if rollback else 'after'], 'Source hash drift')
    changed = False
    try:
        changed = True
        for item in manifest['files']:
            base.atomic_copy(Path(item['backup']) if rollback else ROOT / SOURCE, Path(item['target']))
        # SourceRepository runs in main. X SourceClient is unchanged.
        base.run('systemctl', 'restart', MAIN)
        base.health(config)
        for item in manifest['files']:
            base.require(base.digest(Path(item['target'])) == item['before' if rollback else 'after'],
                         'Installed hash mismatch')
        base.require(pids() == before_pids, 'A continuous service restarted unexpectedly')
        base.require({u: base.unit_state(u) for u in base.TRIGGERS} == before_triggers,
                     'Trigger state changed during deployment')
        manifest['token_fingerprints_unchanged'] = base.token_facts(config) == token_facts
        # Active publishers can advance. Never roll back their ledger or tokens.
        manifest['continuous_services_preserved'] = True
        manifest['triggers_preserved'] = True
        manifest['status'] = 'rolled_back' if rollback else 'deployed'
        base.save(manifest)
    except Exception:
        manifest['status'] = 'recovery_required'
        base.save(manifest)
        if changed:
            for item in manifest['files']:
                base.atomic_copy(ROOT / SOURCE if rollback else Path(item['backup']), Path(item['target']))
            base.run('systemctl', 'restart', MAIN)
            base.health(config)
        manifest['status'] = 'failed_restored'
        base.save(manifest)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rollback', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    config = base.load_baseline()
    targets = [Path(config['paths'][key]) / SOURCE for key in ('main', 'current')]
    with base.deployment_lock(config):
        if args.rollback:
            manifest = json.loads(args.rollback.read_text())
            base.require({i['target'] for i in manifest['files']} == {str(t) for t in targets}, 'Rollback scope mismatch')
            change(config, manifest, True)
        else:
            commit = base.run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
            base.require(not base.run('git', '-C', str(ROOT), 'status', '--porcelain'), 'Dirty source checkout')
            base.require(base.run('git', '-C', str(ROOT), 'ls-remote', 'origin', REF).split() == [commit, REF], 'Commit not exact GitHub ref')
            expected = subprocess.check_output(['git', '-C', str(ROOT), 'show', BASE + ':' + SOURCE])
            for target in targets:
                base.require(target.read_bytes().replace(b'\r\n', b'\n') == expected, 'Live baseline drift: ' + str(target))
            compile((ROOT / SOURCE).read_bytes(), SOURCE, 'exec')
            base.health(config)
            if args.check:
                print(json.dumps({'baseline_verified': True, 'files': len(targets), 'commit': commit}))
                return
            backup = Path(config['paths']['data']) / 'backups' / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-pool-import-' + commit[:8])
            backup.mkdir(mode=0o700)
            manifest = {'version': 1, 'backup': str(backup), 'commit': commit, 'status': 'prepared', 'files': []}
            for index, target in enumerate(targets):
                saved = backup / ('file-' + str(index))
                shutil.copy2(target, saved)
                manifest['files'].append({'target': str(target), 'backup': str(saved),
                                          'before': base.digest(target), 'after': base.digest(ROOT / SOURCE)})
            base.save(manifest)
            change(config, manifest)
        print(json.dumps({'status': manifest['status'], 'manifest': str(Path(manifest['backup']) / 'manifest.json'),
                          'commit': manifest['commit'], 'continuous_services_preserved': manifest.get('continuous_services_preserved')}))


if __name__ == '__main__':
    main()
