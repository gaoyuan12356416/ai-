"""Roll a shared gate forward without stopping admitted SQL connections."""
import json
import os
from pathlib import Path
import subprocess
import time

from . import legacy_sql_gate as legacy
CANONICAL=Path('/run/sql-connection-gate/gate.sock')
UNIT_ALIAS=Path('/etc/systemd/system/sql-connection-gate.service')


def unit_property(unit,property_name):
    return subprocess.check_output(['systemctl','show',unit,'-p',property_name,'--value']).decode().strip()


def promote(*,old_unit,old_run,old_root,new_unit,new_run,new_root):
    old_run,old_root,new_run,new_root=map(Path,(old_run,old_root,new_run,new_root))
    canonical=CANONICAL
    old=legacy.status_request(str(canonical));new=legacy.status_request(str(new_run/'staged.sock'))
    if old['version']!='2.0.0-offload' or old['remote_in_use'] or old['recovered_local_reserved'] or old['legacy_reserved']:
        raise RuntimeError('previous_gate_not_ready_for_upgrade')
    if new['local_in_use'] or new['remote_in_use'] or new['recovered_local_reserved'] or new['legacy_reserved']!=14:
        raise RuntimeError('candidate_not_staged_empty')
    for unit in (old_unit,new_unit):
        if unit_property(unit,'RuntimeDirectoryPreserve')!='yes':raise RuntimeError('runtime_directory_must_be_preserved')
        if unit_property(unit,'SendSIGKILL')!='no':raise RuntimeError('forced_stop_must_be_disabled')
    old_pid=int(unit_property(old_unit,'MainPID'));identity=legacy.read_proc_identity(old_pid)
    if not identity:raise RuntimeError('old_process_missing')
    alias=UNIT_ALIAS
    if not alias.is_symlink() or alias.readlink().name!=old_unit:
        raise RuntimeError('canonical_service_alias_changed')
    previous=new_run/'previous.sock'
    inode=canonical.stat().st_ino
    marker_path=new_root/'handover.json'
    if marker_path.exists():raise RuntimeError('candidate_already_promoted')
    if previous.exists():raise RuntimeError('candidate_previous_socket_exists')
    os.link(str(canonical),str(previous))
    marker=dict(pid=old_pid,starttime=identity['starttime'],bound_path=str(old_run/'staged.sock'),state_path=str(old_run/'state.json'),
                canonical=str(canonical),old_ledger=str(old_root/'local-sessions.json'),old_unit=old_unit,
                shared_pid=int(unit_property(new_unit,'MainPID')),barrier_complete=False,retired=False,
                activated_at=legacy.utc_now_text())
    legacy.durable_replace_json(str(marker_path),marker)
    if canonical.stat().st_ino!=inode:raise RuntimeError('admission_changed_by_other_maintenance')
    os.replace(str(new_run/'staged.sock'),str(canonical))
    # The old socket is still open. An accepted status response is behind all
    # connects made to that socket before the atomic pathname replacement.
    try:
        barrier=legacy.status_request(str(previous))
        if barrier.get('version')!='2.0.0-offload':raise RuntimeError('old_accept_barrier_failed')
    except Exception:
        # No grants can leave the candidate while its marker has no barrier.
        # Restore the still-live old listener without closing accepted sockets.
        if not marker['barrier_complete']:
            os.replace(str(previous),str(canonical))
            marker_path.unlink()
        raise
    marker.update(barrier_complete=True,canonical_inode=canonical.stat().st_ino)
    legacy.durable_replace_json(str(marker_path),marker)
    subprocess.run(['systemctl','stop','--no-block',old_unit],check=True)
    alias_next=alias.with_name(alias.name+'.next');alias_next.symlink_to(new_unit);alias_next.replace(alias)
    state=canonical.parent/'state.json';temporary=state.with_name('shared-state.next')
    temporary.symlink_to(new_run/'state.json');temporary.replace(state)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable',new_unit],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    return dict(stage='draining_previous',previous_pid=old_pid,pid=marker['shared_pid'],marker=str(marker_path))


def finish(new_root):
    marker_path=Path(new_root)/'handover.json'
    marker=json.loads(marker_path.read_text())
    if marker.get('retired'):return True
    state=json.loads(Path(marker['state_path']).read_text())
    sessions=json.loads(Path(marker['old_ledger']).read_text()).get('sessions')
    if state['status']=='stopping' and state['in_use']==0 and state['queued']==0 and not sessions:
        marker.update(retired=True,retired_at=legacy.utc_now_text(),drain_proof='closed_admission_zero_active_zero_queued_empty_ledger')
        legacy.durable_replace_json(str(marker_path),marker)
        return True
    return False
