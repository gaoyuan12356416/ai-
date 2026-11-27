#!/usr/bin/env python3
"""Verify and install report-only WAL access without restarting any publisher."""
import argparse
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNIT = 'post-daily-report.service'
CURRENT = Path('/opt/post-daily-report/current')
FRAGMENT = Path('deploy/post-daily-report.service.d/50-live-ledgers-read.conf')
TARGET = Path('/etc/systemd/system/post-daily-report.service.d/50-live-ledgers-read.conf')
STATE = Path('/mnt/data-disk/post-daily-report')
UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
PROPERTIES = ('ProtectSystem', 'ProtectHome', 'ReadWritePaths', 'ReadOnlyPaths',
              'PrivateTmp', 'NoNewPrivileges')


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_hashes(day):
    paths = list((STATE / 'reports' / day).glob('*')) + list(STATE.glob('delivery.sqlite3*'))
    return {str(p): sha(p) for p in paths if p.is_file()}


def unit_properties():
    return {key: command('systemctl', 'show', UNIT, '-p', key, '--value')
            for key in PROPERTIES}


def additions():
    values = {'ReadWritePaths': [], 'ReadOnlyPaths': []}
    for line in (ROOT / FRAGMENT).read_text().splitlines():
        key, _, value = line.partition('=')
        if key in values:
            values[key].append(value)
    return values


def sandbox(folder, label, argv, extra=None):
    props = unit_properties()
    props['ReadWritePaths'] += ' ' + str(folder)
    for key, values in (extra or {}).items():
        props[key] = props.get(key, '') + ' ' + ' '.join(values)
    args = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect',
            '--unit=post-report-wal-' + label + '-' + str(os.getpid())]
    for key, value in props.items():
        if value.strip():
            args += ['-p', key + '=' + value.strip()]
    result = subprocess.run(args + argv, capture_output=True, text=True, timeout=180)
    (folder / (label + '.log')).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(label + '_failed; see ' + str(folder / (label + '.log')))
    return result.stdout


def preflight(folder, day, commit):
    source = str(CURRENT.resolve())
    before = archive_hashes(day)
    properties = unit_properties()
    fixture_dir = folder / ('fixture-' + str(os.getpid()))
    fixture_dir.mkdir()
    fixture = fixture_dir / 'publisher.sqlite3'
    db = sqlite3.connect(fixture)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE facts(value)')
    db.execute('INSERT INTO facts VALUES(7)')
    db.commit()
    db.close()
    assert not Path(str(fixture) + '-shm').exists()
    fixture_hash = sha(fixture)
    probe = '''import sys,sqlite3,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from features.post_daily_report.common import read_db
path=Path(sys.argv[2]); fixed=sys.argv[3]=='fixed'
try:
 with read_db(path) as db:
  assert db.execute('SELECT value FROM facts').fetchone()[0]==7
  assert fixed, 'restricted WAL read unexpectedly succeeded'
  try: db.execute('UPDATE facts SET value=0')
  except sqlite3.OperationalError: pass
  else: raise AssertionError('query-only protection missing')
except sqlite3.OperationalError as exc:
 if fixed or str(exc) not in ('attempt to write a readonly database','unable to open database file'): raise
 print(json.dumps({'restricted_wal_error':str(exc)}))
else:
 try:
  with path.open('r+b'): pass
 except OSError as exc:
  assert exc.errno==30, exc
 else: raise AssertionError('main database is writable at OS level')
 print(json.dumps({'wal_read':'ok','query_only':True,'main_database_read_only':True}))
'''
    argv = ['/usr/bin/python3', '-c', probe, source, str(fixture)]
    sandbox(folder, 'restricted', argv + ['restricted'], {'ReadOnlyPaths': [str(fixture_dir)]})
    sandbox(folder, 'fixed', argv + ['fixed'],
            {'ReadWritePaths': [str(fixture_dir)], 'ReadOnlyPaths': [str(fixture)]})
    assert sha(fixture) == fixture_hash, 'fixture database changed'
    preview_state = folder / 'validation'
    sandbox(folder, 'preview', ['/usr/bin/python3', str(CURRENT / 'scripts/post_daily_report.py'),
                               '--preview', '--date', day, '--state-dir', str(preview_state)], additions())
    report_path = preview_state / 'previews' / day / 'report.json'
    report = json.loads(report_path.read_text())
    summary = {}
    for channel in report['channels']:
        name = channel['channel']
        rows = channel.get('rows', [])
        available = not channel.get('error') and all(row.get('data_available', True) for row in rows)
        if name in ('TT', 'FB', 'X') and not available:
            raise RuntimeError(name + '_collector_still_unavailable')
        summary[name] = {
            'data_available': available,
            'expected': sum(row['expected'] for row in rows) if available and all(row.get('expected') is not None for row in rows) else None,
            'confirmed': sum(row.get('published', 0) + row.get('late', 0) for row in rows) if available else None,
            'policy_skipped': sum(row.get('policy_skipped', 0) for row in rows) if available else None,
            'warnings': channel.get('warnings', []),
        }
        if summary[name]['expected'] is not None:
            summary[name]['unfinished'] = summary[name]['expected'] - summary[name]['confirmed'] - summary[name]['policy_skipped']
    assert archive_hashes(day) == before, 'original report or delivery ledger changed'
    acceptance = {'commit': commit, 'source': source, 'date': day, 'cutoff_utc': report['cutoff_utc'],
                  'fragment_sha256': sha(ROOT / FRAGMENT), 'properties_before': properties,
                  'original_hashes': before, 'summary': summary,
                  'checks': ['missing WAL auxiliary files reproduced', 'scoped WAL reads succeeded',
                             'main database OS read-only', 'query_only preserved',
                             'original report and delivery preserved', 'production no-send preview']}
    (folder / 'acceptance.json').write_text(json.dumps(acceptance, ensure_ascii=False, indent=2))
    print(json.dumps({'status': 'verified', 'deployment': str(folder), 'summary': summary}, ensure_ascii=False))


def apply(folder, commit):
    accepted = json.loads((folder / 'acceptance.json').read_text())
    assert accepted['commit'] == commit and accepted['source'] == str(CURRENT.resolve())
    assert accepted['fragment_sha256'] == sha(ROOT / FRAGMENT)
    desired = (ROOT / FRAGMENT).read_bytes()
    if TARGET.exists() and TARGET.read_bytes() == desired:
        print(json.dumps({'status': 'already_installed', 'deployment': str(folder)}))
        return
    assert unit_properties() == accepted['properties_before'], 'report unit changed; review and revalidate'
    backup = folder / 'before'
    backup.mkdir(exist_ok=True)
    (backup / 'unit.txt').write_text(command('systemctl', 'cat', UNIT))
    (backup / 'timer.json').write_text(command('systemctl', 'show', 'post-daily-report.timer',
                                             '-p', 'ActiveState', '-p', 'NextElapseUSecRealtime', '-p', 'TimersCalendar'))
    if TARGET.exists():
        (backup / TARGET.name).write_bytes(TARGET.read_bytes())
        rollback = 'cp -- ' + shlex.quote(str(backup / TARGET.name)) + ' ' + shlex.quote(str(TARGET))
    else:
        rollback = 'rm -f -- ' + shlex.quote(str(TARGET))
    rollback_path = folder / 'rollback.sh'
    rollback_path.write_text('#!/bin/bash\nset -euo pipefail\n' + rollback + '\nsystemctl daemon-reload\n')
    rollback_path.chmod(0o700)
    TARGET.parent.mkdir(exist_ok=True)
    temporary = TARGET.with_suffix('.tmp')
    temporary.write_bytes(desired)
    os.replace(temporary, TARGET)
    try:
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        actual = unit_properties()
        for key, values in additions().items():
            assert all(value in actual[key].split() for value in values)
        assert actual['ProtectSystem'] == 'strict'
        assert archive_hashes(accepted['date']) == accepted['original_hashes']
    except Exception:
        subprocess.run(['bash', str(rollback_path)], check=True)
        raise
    (folder / 'installed.json').write_text(json.dumps({'commit': commit, 'fragment_sha256': sha(TARGET),
                                                      'properties_after': actual, 'restarted_services': []}, indent=2))
    print(json.dumps({'status': 'installed', 'commit': commit, 'deployment': str(folder),
                      'rollback': 'bash ' + str(rollback_path), 'restarted_services': []}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    assert command('git', '-C', str(ROOT), 'rev-parse', 'HEAD') == args.commit
    assert command('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == UUID
    folder = Path('/mnt/data-disk/post-daily-wal-20261127') / args.commit[:12]
    folder.mkdir(parents=True, exist_ok=True)
    if args.apply:
        apply(folder, args.commit)
    else:
        preflight(folder, args.date, args.commit)


if __name__ == '__main__':
    main()
