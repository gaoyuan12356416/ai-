#!/usr/bin/env python3
"""Allow SQLite WAL reader coordination only for the independent gateway."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_x_tag_publish as lane
from scripts.deploy_x_hierarchy import mount_check, source_commit, record

TARGET = Path('/etc/systemd/system/x-tag-gateway.service.d/sqlite-users.conf')
BACKUPS = lane.ROOT / 'gateway-storage-backups'


def restart():
    # Prior gateway versions lack graceful SIGTERM. Wait for its own short
    # control requests, without stopping planners, queues or other services.
    deadline = time.monotonic() + 120
    while lane.cmd('ss', '-Hnt', 'state', 'established', '( sport = :8898 )'):
        if time.monotonic() >= deadline:
            raise RuntimeError('gateway_request_active:preserved_retry_later')
        time.sleep(1)
    lane.cmd('systemctl', 'daemon-reload')
    lane.cmd('systemctl', 'restart', 'x-tag-gateway.service', timeout=60)
    lane.health(8898, '/healthz')


def restore(info):
    if lane.sha(TARGET) not in (info['before'], info['after']):
        raise RuntimeError('later_gateway_unit_change')
    if info['before'] is None:
        TARGET.unlink(missing_ok=True)
    else:
        lane.atomic(TARGET, (Path(info['backup']) / 'before.conf').read_bytes())
    restart()


def install():
    mount_check()
    commit = source_commit()
    source = SOURCE / 'deploy/x-tag-gateway-sqlite.conf'
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    info = dict(commit=commit, backup=str(backup), target=str(TARGET), before=lane.sha(TARGET),
                after=lane.sha(source), legacy_before=lane.pid_state(), status='prepared')
    if TARGET.exists():
        lane.atomic(backup / 'before.conf', TARGET.read_bytes())
    record(backup / 'deployment.json', info)
    try:
        lane.atomic(TARGET, source.read_bytes())
        restart()
        info.update(status='active', legacy_after=lane.pid_state())
        info['legacy_pids_unchanged'] = info['legacy_before'] == info['legacy_after']
    except Exception:
        restore(info)
        info['status'] = 'reverted_config_only'
        raise
    finally:
        record(backup / 'deployment.json', info)
    print(json.dumps(info), flush=True)


def rollback(backup):
    mount_check()
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve():
        raise RuntimeError('invalid_backup')
    info = json.loads((backup / 'deployment.json').read_text())
    restore(info)
    print(json.dumps(dict(status='rolled_back_config_only', backup=str(backup))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'rollback'))
    parser.add_argument('--backup')
    args = parser.parse_args()
    install() if args.action == 'install' else rollback(args.backup)
