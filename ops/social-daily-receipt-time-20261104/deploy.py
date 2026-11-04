"""Stage/activate one reporting adapter; never restore publisher or delivery state."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
STATE = Path('/mnt/data-disk/social-daily-receipt-time-20261104')
LINK = Path('/opt/youtube-publisher-daily-report/current')
FILE = Path('features/social_analytics/facebook.py')
BEFORE = '1e5757e3b486f397e6b850330e12dbbef692525be630697d49e50f558deb4289'
LEDGER = Path('/mnt/data-disk/social-analytics/attribution.sqlite3')
REPORTS = Path('/mnt/data-disk/youtube-publisher-daily-report')


def run(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(source, target):
    with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True) as src:
        with sqlite3.connect(target) as dest:
            src.backup(dest)


def history():
    return {str(p.relative_to(REPORTS)): sha(p)
            for p in (REPORTS / 'reports').rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['stage', 'canary', 'apply', 'rollback'])
    action = parser.parse_args().action
    if run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_not_mounted')
    if shutil.disk_usage(STATE.parent).free < 1024**3:
        raise RuntimeError('insufficient_space')
    os.umask(0o077)
    STATE.mkdir(exist_ok=True)
    manifest = STATE / 'deployment.json'
    if action == 'stage':
        if manifest.exists():
            raise RuntimeError('already_staged')
        commit = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
        if run('git', '-C', str(ROOT), 'status', '--porcelain'):
            raise RuntimeError('dirty_source')
        if not run('git', '-C', str(ROOT), 'branch', '-r', '--contains', commit):
            raise RuntimeError('source_not_on_github')
        previous = LINK.resolve()
        if sha(previous / FILE) != BEFORE:
            raise RuntimeError('report_baseline_changed')
        release = previous.parent / (commit[:12] + '-receipt-time')
        shutil.copytree(previous, release, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(ROOT / FILE, release / FILE)
        compile((release / FILE).read_text(), str(release / FILE), 'exec')
        snapshot(LEDGER, STATE / 'attribution.canary.sqlite3')
        state = dict(commit=commit, previous=str(previous), release=str(release),
                     before=BEFORE, after=sha(release / FILE), history=history(), status='staged')
    else:
        state = json.loads(manifest.read_text())
        release = Path(state['release'])
        if sha(release / FILE) != state['after']:
            raise RuntimeError('release_changed')
        if action == 'canary':
            sys.path.insert(0, str(release))
            from features.social_analytics.facebook import collect
            candidate = STATE / 'attribution.canary.sqlite3'
            with sqlite3.connect(candidate) as conn:
                conn.row_factory = sqlite3.Row
                before = {(r['site_id'], r['record_id']): dict(r)
                          for r in conn.execute('SELECT * FROM social_facebook_identity')}
            result = collect(ledger=candidate)
            if result['status'] != 'complete':
                raise RuntimeError('canary_collection_incomplete:' + json.dumps(result))
            repaired = []
            with sqlite3.connect(candidate) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute('SELECT * FROM social_facebook_identity'):
                    old = before.get((row['site_id'], row['record_id']))
                    if not old:
                        continue
                    for field in ('identity_json', 'evidence_json', 'evidence_hash', 'frozen_at'):
                        if old[field] != row[field]:
                            raise RuntimeError('frozen_identity_changed')
                    if old['publications_json'] != '[]' and old['publications_json'] != row['publications_json']:
                        raise RuntimeError('frozen_publication_changed')
                    if old['status'] == 'conflict' and row['status'] == 'frozen':
                        if old['error_code'] != 'publication_time_mismatch':
                            raise RuntimeError('unexpected_conflict_recovery')
                        repaired.append(row['record_id'])
            if history() != state['history']:
                raise RuntimeError('historical_reports_changed')
            evidence = dict(status='complete', after=state['after'], repaired_ids=repaired,
                            repaired_count=len(repaired), collection=result)
            (STATE / 'canary.json').write_text(json.dumps(evidence, indent=2))
            print(json.dumps(evidence))
            return
        forward = action == 'apply'
        if str(LINK.resolve()) != state['previous' if forward else 'release']:
            raise RuntimeError('release_pointer_changed')
        if forward:
            evidence = json.loads((STATE / 'canary.json').read_text())
            if evidence['status'] != 'complete' or evidence['after'] != state['after']:
                raise RuntimeError('canary_not_verified')
        with (REPORTS / 'run.lock').open('a') as report_lock, LEDGER.with_suffix('.collector.lock').open('a') as collector_lock:
            fcntl.flock(report_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(collector_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if forward:
                snapshot(LEDGER, STATE / 'attribution.before-apply.sqlite3')
                snapshot(REPORTS / 'delivery.sqlite3', STATE / 'delivery.before-apply.sqlite3')
                if history() != state['history']:
                    raise RuntimeError('historical_reports_changed')
            temporary = LINK.with_name('current.receipt-time-next')
            temporary.symlink_to(state['release' if forward else 'previous'])
            os.replace(temporary, LINK)
        state['status'] = 'active' if forward else 'rolled_back'
    manifest.write_text(json.dumps(state, indent=2) + '\n')
    print(json.dumps({k: v for k, v in state.items() if k != 'history'}))


if __name__ == '__main__':
    main()
