"""Stage scoped overlays and switch only after dependent claims finish.

Run from an exact GitHub checkout. The private manifest names current symlinks,
units and the reviewed old hashes; unrelated code is copied from each live root.
The retained deployment directory is the rollback record; business DBs are never
restored by code rollback.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.pipeline_service_drain import DB, pending
from features.pipeline_dispatch.maintenance import UNITS

# The X service already has a later 92-pipeline-graceful.conf setting 6h.
# This narrow override must sort after it; otherwise systemd can still kill a
# sufficiently long upload despite the ExecStop barrier.
TIMEOUT_OVERRIDE = Path('/etc/systemd/system/x-post-automation.service.d/99-upload-continuity-timeout.conf')


def install_timeout_override():
    TIMEOUT_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
    TIMEOUT_OVERRIDE.write_text('[Service]\nTimeoutStopSec=infinity\n')


def run(*args):
    subprocess.run(args, check=True)


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(path)


def emit(**value):
    print(json.dumps(value), flush=True)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def link(target, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.continuity-new')
    tmp.symlink_to(target)
    tmp.replace(path)


def config_pause(record, folder):
    with contextlib.closing(sqlite3.connect(DB, timeout=30)) as c:
        c.execute('BEGIN IMMEDIATE')
        version, raw = c.execute('SELECT version,config_json FROM pipeline_config WHERE id=1').fetchone()
        cfg = json.loads(raw)
        record['enabled_channels_before'] = cfg['enabled_channels']
        record['inflight_before'] = pending('pipeline-coordinator.service')
        save(folder/'record.json', record)
        cfg['enabled_channels'] = []
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        c.execute('UPDATE pipeline_config SET version=?,config_json=?,updated_at=? WHERE id=1',
                  (version+1, json.dumps(cfg), now))
        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(NULL,?,?,?)',
                  ('service_switch_admission_paused', json.dumps({'deployment': str(folder), 'previous': record['enabled_channels_before']}), now))
        c.commit()
    emit(admission_paused=True, inflight=len(record['inflight_before']))


def config_resume(record):
    with contextlib.closing(sqlite3.connect(DB, timeout=30)) as c:
        c.execute('BEGIN IMMEDIATE')
        version, raw = c.execute('SELECT version,config_json FROM pipeline_config WHERE id=1').fetchone()
        cfg = json.loads(raw)
        if cfg['enabled_channels'] != []:
            raise RuntimeError('concurrent_channel_configuration_changed')
        cfg['enabled_channels'] = record['enabled_channels_before']
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        c.execute('UPDATE pipeline_config SET version=?,config_json=?,updated_at=? WHERE id=1',
                  (version+1, json.dumps(cfg), now))
        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(NULL,?,?,?)',
                  ('service_switch_admission_restored', json.dumps({'channels': cfg['enabled_channels']}), now))
        c.commit()


def prepare(manifest, folder):
    # Service users must traverse to their immutable candidate code. Private
    # plans, configuration snapshots and records have their own mode 0600.
    folder.mkdir(parents=True, mode=0o755, exist_ok=False)
    revision = subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD']).decode().strip()
    record = {'revision': revision, 'source': str(ROOT), 'lanes': [], 'state': 'preparing', 'dropins': {},
              'timeout_override_before': TIMEOUT_OVERRIDE.read_text() if TIMEOUT_OVERRIDE.exists() else None}
    for entry in manifest['lanes']:
        pointer = Path(entry['pointer'])
        old = pointer.resolve(strict=True)
        for name, expected in entry['old_hashes'].items():
            if sha(old/name) != expected:
                raise RuntimeError('concurrent_file_change:' + entry['lane'] + ':' + name)
        new = folder/'candidates'/entry['lane']
        new.parent.mkdir(exist_ok=True)
        run('cp','-a','--reflink=auto',str(old),str(new))
        for name in entry['files']:
            (new/name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT/name, new/name)
        record['lanes'].append(dict(entry, previous=str(old), candidate=str(new)))
    for unit in UNITS:
        path = Path('/etc/systemd/system')/(unit+'.d')/'70-upload-continuity.conf'
        record['dropins'][unit] = path.read_text() if path.exists() else None
    for lane in ('x','coordinator'):
        run('python3','/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py',
            'snapshot','--lane',lane,'--output',str(folder/('schedulers-before-'+lane+'.json')))
    for lane in record['lanes']:
        env = dict(os.environ, PYTHONPATH=lane['candidate'])
        files = [str(Path(lane['candidate'])/name) for name in lane['files'] if name.endswith('.py')]
        subprocess.run(['python3','-m','py_compile',*files],check=True,env=env)
    record['state'] = 'prepared'
    save(folder/'record.json', record)
    emit(state='prepared', revision=revision, directory=str(folder))


def activate(folder, rollback=False, resume=False):
    record = json.loads((folder/'record.json').read_text())
    if not rollback and record['state'] != 'prepared':
        raise RuntimeError('deployment_not_prepared')
    # Bootstrap is needed only to activate the first coordinator that knows
    # the systemd admission hold. Every live claim still heartbeats/completes.
    if resume:
        with contextlib.closing(sqlite3.connect(DB,timeout=30)) as c:
            cfg=json.loads(c.execute('SELECT config_json FROM pipeline_config WHERE id=1').fetchone()[0])
        if cfg['enabled_channels'] != [] or not record.get('enabled_channels_before'):
            raise RuntimeError('resume_requires_original_admission_pause')
    else:
        config_pause(record, folder)
    previous = None
    while True:
        jobs = pending('pipeline-coordinator.service')
        signature = [(j['id'],j['generation']) for j in jobs]
        if signature != previous:
            emit(draining=jobs)
            previous = signature
        if not jobs:
            break
        time.sleep(2)
    if not rollback:
        link(ROOT, Path('/opt/x-upload-continuity/current'))
        for unit in UNITS:
            path = Path('/etc/systemd/system')/(unit+'.d')/'70-upload-continuity.conf'
            path.parent.mkdir(parents=True,exist_ok=True)
            helper = '/usr/bin/python3 /opt/x-upload-continuity/current/scripts/pipeline_service_drain.py'
            path.write_text('[Service]\nExecStop=+'+helper+' stop --unit='+unit+'\n'
                            'ExecStartPost=+'+helper+' start --unit='+unit+'\nTimeoutStopSec=infinity\n')
        install_timeout_override()
        run('systemctl','daemon-reload')
    # Keep admission paused until all three services are ready; there are no
    # worker restarts, resets, lock expiry shortcuts, or external write retries.
    for entry in record['lanes']:
        pointer = Path(entry['pointer'])
        if resume and str(pointer.resolve()) == entry['candidate']:
            pid=subprocess.check_output(['systemctl','show',entry['unit'],'-p','MainPID','--value']).decode().strip()
            if not pid.isdigit() or int(pid)<2 or (Path('/proc')/pid/'cwd').resolve() != Path(entry['candidate']).resolve():
                raise RuntimeError('candidate_pointer_not_running:' + entry['lane'])
            emit(already_running=entry['unit'])
            continue
        expected = entry['candidate'] if rollback else entry['previous']
        if str(pointer.resolve()) != expected:
            raise RuntimeError('concurrent_release_change:' + entry['lane'])
        run('systemctl','stop',entry['unit'])
        link(entry['previous'] if rollback else entry['candidate'], pointer)
        run('systemctl','start',entry['unit'])
        emit(service_ready=entry['unit'])
    config_resume(record)
    # Keep the guard installed during rollback: the old coordinator lacks hold
    # support, so rollback requires the same admission-pause switch script.
    if rollback:
        for unit, old in record['dropins'].items():
            path = Path('/etc/systemd/system')/(unit+'.d')/'70-upload-continuity.conf'
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(old)
        old_timeout = record.get('timeout_override_before')
        if old_timeout is None:
            TIMEOUT_OVERRIDE.unlink(missing_ok=True)
        else:
            TIMEOUT_OVERRIDE.write_text(old_timeout)
        run('systemctl','daemon-reload')
    for lane in ('x','coordinator'):
        run('python3','/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py',
            'restore','--baseline',str(folder/('schedulers-before-'+lane+'.json')),
            '--wait-seconds','25','--output',str(folder/('schedulers-after-'+lane+'.json')))
    # A separately observed native intake runner may need its new triggers
    # paused while existing calls finish. Restore only the recorded active
    # timer/path triggers; never directly start its publishing oneshot.
    triggers=folder/'tag-triggers-before.json'
    if triggers.exists():
        before=json.loads(triggers.read_text())['states'];after={}
        for unit,original in before.items():
            if unit not in ('x-tag-auto-runner.timer','x-tag-auto-runner.path'):
                raise RuntimeError('unexpected_intake_trigger')
            enabled=subprocess.check_output(['systemctl','show',unit,'-p','UnitFileState','--value']).decode().strip()
            if original['ActiveState']=='active' and enabled==original['UnitFileState']:
                run('systemctl','start',unit)
            after[unit]=subprocess.check_output(['systemctl','show',unit,'-p','ActiveState','--value']).decode().strip()
        save(folder/'tag-triggers-after.json',after)
    record['state'] = 'rolled_back' if rollback else 'active'
    record['controller_revision']=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD']).decode().strip()
    record['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    save(folder/'record.json', record)
    emit(state=record['state'])


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','activate','resume','rollback'))
    p.add_argument('--directory',required=True);p.add_argument('--manifest')
    a=p.parse_args()
    if a.action=='prepare': prepare(json.loads(Path(a.manifest).read_text()),Path(a.directory))
    else: activate(Path(a.directory),rollback=a.action=='rollback',resume=a.action=='resume')
