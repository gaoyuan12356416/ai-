"""Collect one Campaign worker's pre-authentication failure from its systemd journal."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def worker_proof(lease, heartbeat):
    if not re.fullmatch(r'[0-9a-f]{32}', lease) or not 0 < heartbeat < time.time():
        raise ValueError('invalid incident')
    unit = 'ai-backend-offload-campaign-refresh.service'
    args = ['journalctl', '-u', unit, '--since', '@' + str(int(heartbeat) - 8),
            '--until', '@' + str(int(heartbeat) + 33), '-o', 'json', '--no-pager', '-n', '180']
    rows = [json.loads(line) for line in subprocess.check_output(args, timeout=8).splitlines()]
    marked = []
    for entry in rows:
        try:
            message = json.loads(entry['MESSAGE'])
        except (ValueError, KeyError):
            continue
        if (isinstance(message, dict) and message.get('event') == 'offload_sql_close_unconfirmed'
                and message.get('lease') == lease and message.get('task') == 'campaign-copy-report'
                and message.get('locally_aborted') is False):
            marked.append(entry)
    if len(marked) != 1:
        raise ValueError('missing unique Campaign close event')

    record = marked[0]
    invocation, pid = record['_SYSTEMD_INVOCATION_ID'], int(record['_PID'])
    trace = '\n'.join(row['MESSAGE'] for row in rows if row.get('_SYSTEMD_INVOCATION_ID') == invocation
                      and row.get('_PID') == str(pid))
    needed = ('in _do_handshake', '_socket.recv()', 'header = self._recv_chunk',
              'mysql.connector.errors.ConnectionTimeoutError')
    if not all(part in trace for part in needed) or any(part in trace for part in ('in _do_auth', 'in cmd_query')):
        raise ValueError('missing first-header timeout before authentication')

    now = time.time()
    stat = Path('/proc') / str(pid) / 'stat'
    try:
        ticks = int(stat.read_text().rsplit(')', 1)[1].split()[19])
    except FileNotFoundError:
        exited = True
    else:
        boot = now - float(Path('/proc/uptime').read_text().split()[0])
        exited = boot + ticks / os.sysconf('SC_CLK_TCK') > heartbeat + 60
    sockets = subprocess.check_output(['ss', '-Hnt', '( sport = :19969 or dport = :19969 )'], timeout=5)
    if not exited or sockets.strip():
        raise ValueError('worker or data transport still present')
    return dict(lease=lease, node='hk-read-assist', task='campaign-copy-report', observed_at=now,
                failure_at=int(record['__REALTIME_TIMESTAMP']) / 1e6, preauth_before_first_header=True,
                worker_exited=True, open_data_sockets=0, pid=pid, invocation=invocation,
                journal_hash=hashlib.sha256(trace.encode()).hexdigest())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lease')
    parser.add_argument('heartbeat', type=float)
    args = parser.parse_args()
    print(json.dumps(worker_proof(args.lease, args.heartbeat)))
