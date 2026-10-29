#!/usr/bin/env python3
"""Install the throughput repair without restarting the main API."""
import argparse
import json
from pathlib import Path

from install_cover import MODULE, atomic, run
from install_cover_report import apply as switch_report


def apply(backup, rollback=False):
    if rollback:
        run(['systemctl', 'stop', 'meta-cover-worker.service'])
    switch_report(backup, rollback)
    plan = json.loads((backup / 'plan.json').read_text())
    source = backup if rollback else Path(plan['release']) / MODULE / 'deploy'
    atomic(source / 'meta-cover-worker.service', Path('/etc/systemd/system/meta-cover-worker.service'))
    run(['systemctl', 'daemon-reload'])
    # A bounded acceptance run precedes continuous activation. Rollback also
    # leaves execution stopped, preserving current outcomes and unknown fences.
    print(json.dumps({'worker_unit_updated': True, 'worker_started': False,
                      'backup': str(backup), 'rollback': rollback}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('apply', 'rollback'))
    parser.add_argument('backup', type=Path)
    args = parser.parse_args()
    apply(args.backup.resolve(), args.mode == 'rollback')
