"""Switch only admission; retire the old daemon only after its leases finish."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from . import legacy_sql_gate as legacy
from .shared_sql_gate import legacy_occupancy

OLD_UNIT='sql-connection-gate.service'
NEW_UNIT='ai-backend-shared-sql.service'
UNIT_PATH=Path('/etc/systemd/system')/OLD_UNIT
DROPIN=Path('/etc/systemd/system/sql-connection-gate.service.d/90-offload-handover.conf')
CANONICAL=Path('/run/sql-connection-gate/gate.sock')
STAGED=Path('/run/sql-shared-gate/staged.sock')
ALIAS=Path('/run/sql-shared-gate/legacy.sock')
LEGACY_STATE=Path('/run/sql-connection-gate/state.json')
LEGACY_LEDGER=Path('/var/lib/sql-connection-gate/active-sessions.json')


def systemctl(*args):
    return subprocess.check_output(['systemctl']+list(args),stderr=subprocess.PIPE).decode().strip()


def activate(root):
    root=Path(root);marker_path=root/'handover.json'
    if marker_path.exists():raise RuntimeError('handover_already_recorded')
    old_pid=int(systemctl('show',OLD_UNIT,'-p','MainPID','--value'))
    identity=legacy.read_proc_identity(old_pid)
    if not identity:raise RuntimeError('old_gate_not_running')
    old_status=legacy.status_request(str(CANONICAL))
    candidate=legacy.status_request(str(STAGED))
    if old_status['version']!='1.1.0' or old_status['capacity']!=14:raise RuntimeError('old_gate_version_changed')
    if (candidate['version']!='2.0.0-offload' or candidate['local_in_use'] or candidate['remote_in_use']
            or candidate['legacy_reserved']!=14):raise RuntimeError('candidate_not_staged_empty')
    backup=root/'legacy-backup';backup.mkdir(mode=0o700)
    old_unit=UNIT_PATH.read_bytes()
    (backup/'sql-connection-gate.service').write_bytes(old_unit)
    (backup/'state.json').write_bytes(LEGACY_STATE.read_bytes())
    if DROPIN.exists():raise RuntimeError('handover_dropin_already_exists')
    DROPIN.parent.mkdir(exist_ok=True)
    DROPIN.write_text('[Service]\nRestart=no\n')
    systemctl('daemon-reload')
    old_inode=CANONICAL.stat().st_ino
    os.link(str(CANONICAL),str(ALIAS))
    marker=dict(pid=old_pid,starttime=identity['starttime'],bound_path=str(CANONICAL),canonical=str(CANONICAL),
                alias=str(ALIAS),state_path=str(LEGACY_STATE),barrier_complete=False,
                retired=False,old_inode=old_inode,old_unit_sha256=hashlib.sha256(old_unit).hexdigest(),
                backup=str(backup),activated_at=legacy.utc_now_text())
    legacy.durable_replace_json(str(marker_path),marker)
    replaced=False
    try:
        if CANONICAL.stat().st_ino!=old_inode or legacy.read_proc_identity(old_pid)!=identity:
            # State may naturally change R/S, so compare the stable identity below.
            current=legacy.read_proc_identity(old_pid)
            if CANONICAL.stat().st_ino!=old_inode or not current or current['starttime']!=identity['starttime']:
                raise RuntimeError('old_gate_identity_changed')
        os.replace(str(STAGED),str(CANONICAL));replaced=True
        # This request is behind the old listener's pre-switch accept backlog.
        barrier=legacy.status_request(str(ALIAS))
        if barrier['capacity']!=14:raise RuntimeError('legacy_budget_changed')
        marker.update(barrier_complete=True,canonical_inode=CANONICAL.stat().st_ino,
                      shared_pid=int(systemctl('show',NEW_UNIT,'-p','MainPID','--value')))
        legacy.durable_replace_json(str(marker_path),marker)
    except Exception:
        if not marker.get('barrier_complete'):
            # No grants were possible with all fourteen old slots reserved.
            if replaced:
                restore=ALIAS.with_name('restore.sock');os.link(str(ALIAS),str(restore));os.replace(str(restore),str(CANONICAL))
            marker_path.unlink();ALIAS.unlink();DROPIN.unlink();systemctl('daemon-reload')
        raise
    return dict(stage='legacy_draining',old_pid=old_pid,shared_pid=marker['shared_pid'],old_reserved=legacy_occupancy(marker),marker=str(marker_path))


def retire(root):
    root=Path(root);marker_path=root/'handover.json';marker=json.loads(marker_path.read_text())
    if marker.get('retired'):return {'stage':'already_retired'}
    if not marker.get('barrier_complete'):raise RuntimeError('accept_barrier_not_complete')
    if CANONICAL.stat().st_ino!=marker['canonical_inode']:raise RuntimeError('canonical_gate_changed')
    if hashlib.sha256(UNIT_PATH.read_bytes()).hexdigest()!=marker['old_unit_sha256']:
        raise RuntimeError('old_unit_changed_by_other_maintenance')
    for _ in range(2):
        if legacy_occupancy(marker):return {'stage':'legacy_draining','reserved':legacy_occupancy(marker)}
        ledger=json.loads(LEGACY_LEDGER.read_text())
        if ledger.get('sessions'):raise RuntimeError('legacy_sessions_not_empty')
        time.sleep(0.25)
    if DROPIN.read_text()!='[Service]\nRestart=no\n':raise RuntimeError('handover_dropin_changed')
    # The idle legacy daemon unlinks the canonical socket in its SIGTERM cleanup.
    # Skip that destructive cleanup only after no accepted/queued/active lease
    # or durable child session remains. No query process receives this signal.
    DROPIN.write_text('[Service]\nRestart=no\nKillSignal=SIGKILL\nTimeoutStopSec=5\n')
    systemctl('daemon-reload')
    if legacy_occupancy(marker):raise RuntimeError('legacy_became_active_before_retirement')
    systemctl('stop',OLD_UNIT)
    if legacy.process_exists(marker['pid']):raise RuntimeError('idle_legacy_daemon_still_alive')
    marker.update(retired=True,retired_at=legacy.utc_now_text());legacy.durable_replace_json(str(marker_path),marker)
    # Remove the temporary signal override before the old service name becomes
    # an alias of the new service; otherwise it would affect the new daemon.
    DROPIN.unlink();UNIT_PATH.unlink();UNIT_PATH.symlink_to(NEW_UNIT)
    state=Path(marker['state_path']);replacement=state.with_name('shared-state.next')
    replacement.symlink_to('/run/sql-shared-gate/state.json');replacement.replace(state)
    systemctl('daemon-reload');systemctl('enable',NEW_UNIT)
    return dict(stage='shared_active',pid=marker['shared_pid'],capacity=14,remote_enabled=False,backup=marker['backup'])


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('operation',choices=('activate','retire'));parser.add_argument('--root',required=True)
    args=parser.parse_args();print(json.dumps((activate if args.operation=='activate' else retire)(args.root)))


if __name__=='__main__':main()
