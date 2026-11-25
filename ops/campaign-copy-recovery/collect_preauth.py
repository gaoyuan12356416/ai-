"""Check for unbound Campaign incidents on the CPU coordinator and prepare a dry-run receipt."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import time

from cloud_sessions import observe_cluster
from reconcile_preauth import reconcile, require


ROOT = Path('/mnt/data-disk/gpu-backend-offload-20261123')
BOOK = ROOT / 'sql-teal-state/remote-leases.sqlite3'
GATE = Path('/run/sql-connection-gate/state.json')
WATCH = Path('/var/lib/sql-query-watchdog/state.json')
ENDPOINT = '101.32.56.53:63350'


def pending(book):
    with closing(sqlite3.connect(book.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM remote_lease WHERE node='hk-read-assist' AND task='campaign-copy-report' "
                          "AND state='unknown' AND identity IS NULL AND reason='heartbeat_lost' LIMIT 2").fetchall()
    return [dict(row) for row in rows]


def require_room():
    gate = json.loads(GATE.read_bytes())
    require(gate['capacity'] == 14 and gate['remote_in_use'] == 1 and gate['available'] >= 1 and gate['queued'] == 0,
            'no free existing slot for controller verification')


def guarded_endpoint(witness_id):
    require_room()
    values = {}
    for line in Path('/etc/sql-query-watchdog.env').read_text().splitlines():
        key, _, raw = line.partition('=')
        if key.startswith('SQL_WATCHDOG_DB_'):
            values[key] = shlex.split(raw)[0] if raw.strip() else ''
    user = values.get('SQL_WATCHDOG_DB_USER', 'ads_aius')
    require(user == 'ads_aius' and values.get('SQL_WATCHDOG_DB_HOST', '101.32.56.53') == '101.32.56.53' and
            values.get('SQL_WATCHDOG_DB_PORT', '63350') == '63350', 'read endpoint changed')
    require(not os.environ.get('SQL_GATE_BYPASS'), 'FIFO gate required')
    args = ['/usr/bin/mysql', '--batch', '--raw', '--skip-column-names', '--protocol=TCP', '--connect-timeout=4',
            '--host=101.32.56.53', '--port=63350', '--user=' + user]
    query = ('SET SESSION MAX_EXECUTION_TIME=1500; SET SESSION TRANSACTION READ ONLY; '
             'SELECT @@server_uuid,@@read_only,@@tx_read_only,ID,USER,HOST '
             'FROM information_schema.PROCESSLIST WHERE ID=' + str(int(witness_id)) + ';')
    env = dict(os.environ, MYSQL_PWD=values['SQL_WATCHDOG_DB_PASSWORD'], SQL_GATE_JOB_NAME='campaign-handshake-reconciliation',
               SQL_GATE_JOB='campaign-handshake-reconciliation')
    run = subprocess.run(args, env=env, input=query, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, timeout=24)
    fields = run.stdout.strip().split('\t')
    require(run.returncode == 0 and 'SQL_GATE' in run.stderr and 'granted' in run.stderr and len(fields) == 6,
            'guarded controller observation unavailable')
    require(fields[1:3] == ['1', '1'] and fields[3:5] == [str(witness_id), user], 'controller or endpoint changed')
    live = subprocess.check_output(['ss', '-Hntp', 'dst', ENDPOINT], universal_newlines=True, timeout=5).splitlines()
    unidentified = [line for line in live if 'users:(' not in line and
                    line.split()[0] not in ('TIME-WAIT', 'FIN-WAIT-1', 'FIN-WAIT-2', 'LAST-ACK', 'CLOSING')]
    transports = [line for line in live if '"sshd' in line or '"ssh"' in line]
    require(not unidentified and not transports, 'old relay transport may still be open')
    return dict(observed_at=time.time(), endpoint=ENDPOINT, server_uuid=fields[0], readonly=True, transaction_readonly=True,
                witness_id=witness_id, witness_user=user, witness_host=fields[5], gate_used=True,
                remote_ssh_data_sockets=0, unattributed_sockets=0)


def save_once(path, data):
    encoded = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    if path.exists():
        require(path.read_bytes() == encoded, 'incident evidence already exists with different content')
        return
    with path.open('xb') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())


def collect(worker_file=None, book=BOOK):
    rows = pending(book)
    if worker_file is None or not rows:
        return {'pending': [{'lease': row['id'], 'heartbeat': row['heartbeat']} for row in rows]}
    require(len(rows) == 1, 'need one identifiable Campaign lease')
    row = rows[0]
    stat = worker_file.stat()
    require(stat.st_uid == os.geteuid() == 0 and not stat.st_mode & 0o077 and worker_file.is_file()
            and not worker_file.is_symlink() and stat.st_size < 4096, 'root-private worker observation required')
    worker = json.loads(worker_file.read_bytes())
    require(worker['lease'] == row['id'] and worker['node'] == row['node'] and worker['task'] == row['task']
            and worker['preauth_before_first_header'] is True and worker['worker_exited'] is True
            and worker['open_data_sockets'] == 0 and 0 <= time.time() - worker['observed_at'] < 150,
            'worker evidence does not establish an ended handshake')
    require(0 <= worker['failure_at'] - row['heartbeat'] <= 30, 'incident time mismatch')
    require_room()
    watch = json.loads(WATCH.read_bytes())
    require(watch['healthy'] and watch['connected'], 'controller is unavailable')
    witness_id = int(watch['controller_id'])
    cloud = observe_cluster(row['heartbeat'], witness_id, 'ads_aius')
    proof = dict(worker=worker, endpoint=guarded_endpoint(witness_id), cloud=cloud)
    checked = reconcile(book, proof, time.time())
    incident = ROOT / 'campaign-handshake-recovery/incidents' / row['id']
    incident.mkdir(mode=0o700, exist_ok=True, parents=True)
    require(not incident.stat().st_mode & 0o077, 'private evidence directory required')
    save_once(incident / 'before.json', row)
    target = incident / ('observations-' + str(int(cloud['collected'])) + '.json')
    save_once(target, proof)
    return dict(checked, proof=str(target))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path, help='fresh observation collected on Hong Kong')
    args = parser.parse_args()
    print(json.dumps(collect(args.worker)))
