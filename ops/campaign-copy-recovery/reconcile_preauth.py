"""Close an unidentified Campaign lease only with cross-checked pre-authentication evidence.

This root-only operator command consumes evidence from the worker, the guarded
read endpoint and the managed cluster session inventory. It opens no MySQL
connection. The live admission service continues to own all other leases.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(proof, row, previous, now):
    worker, endpoint, cloud = (proof[key] for key in ('worker', 'endpoint', 'cloud'))
    require(row['id'] == worker['lease'] and re.fullmatch('[a-f0-9]{32}', row['id']), 'wrong lease')
    require(row['node'] == worker['node'] == 'hk-read-assist' and
            row['task'] == worker['task'] == 'campaign-copy-report', 'wrong owner')
    require(row['state'] == 'unknown' and row['identity'] is None and
            row['reason'] == 'heartbeat_lost', 'lease is not an unidentified handshake')
    require(0 <= worker['failure_at'] - row['created'] <= 30 and
            0 <= worker['failure_at'] - row['heartbeat'] <= 30 and
            abs(cloud['incident'] - row['heartbeat']) < 1, 'incident time mismatch')
    require(worker['preauth_before_first_header'] is True and worker['worker_exited'] is True and
            worker['open_data_sockets'] == 0 and re.fullmatch('[a-f0-9]{64}', worker['journal_hash']),
            'worker may have authenticated or still own a connection')
    for stamp in (worker['observed_at'], endpoint['observed_at'], cloud['started_at'], cloud['collected']):
        require(0 <= now - stamp <= 180 and stamp > worker['failure_at'], 'stale observation')
    require(cloud['collected'] >= cloud['started_at'] and 0 < cloud['cut'] < cloud['started_at'] - row['created'],
            'invalid session observation window')
    require(endpoint['endpoint'] == '101.32.56.53:63350' and endpoint['endpoint'] in row['endpoint'] and
            endpoint['readonly'] is True and endpoint['transaction_readonly'] is True and
            endpoint['gate_used'] is True, 'wrong read endpoint')
    require(endpoint['remote_ssh_data_sockets'] == endpoint['unattributed_sockets'] == 0,
            'previous relay transport may still be open')
    require(previous['server_uuid'] == endpoint['server_uuid'] and previous['database_user'] == endpoint['witness_user'] and
            previous['read_only'] == previous['transaction_read_only'] == 1, 'replica does not match previous report session')
    require(re.fullmatch(r'43\.166\.187\.96:[1-9][0-9]*', endpoint['witness_host']) and
            endpoint['witness_id'] > 0, 'missing independent controller identity')

    nodes = cloud['nodes']
    ids = {node['instance'] for node in nodes}
    require(cloud['region'] == 'ap-hongkong' and cloud['cluster'] == 'cynosdbmysql-5kxxsre7' and
            cloud['inventory_receipt'] and len(ids) == len(nodes) and ids == set(cloud['member_ids']) and
            any(n['role'] == 'master' for n in nodes) and any(n['role'] == 'ro' for n in nodes),
            'incomplete managed cluster inventory')
    matches = []
    for node in nodes:
        candidates = set()
        for name in ('old', 'preauth', 'connecting', 'witness'):
            result = node[name]
            require(result['receipt'] and 0 <= result['rows'] < result['limit'] and
                    not result['wrong_time_filter'], 'incomplete managed session listing')
            if name == 'witness':
                require(result['rows'] == len(result['positive']), 'unexpected witness results')
                matches.extend((node['role'], item) for item in result['positive'])
            else:
                candidates.update(str(item['thread']) for item in result['possible_old_preauth'])
        require(set(node['resolved']) == candidates, 'pending handshake was not checked individually')
        for thread in candidates:
            final = node['resolved'][thread]
            require(final['request'] and (final['gone'] is True or final['authenticated'] is True),
                    'a candidate connection is still unauthenticated')
    witness = {'id': endpoint['witness_id'], 'user': endpoint['witness_user'], 'host': endpoint['witness_host']}
    require(matches == [('ro', witness)], 'cloud view does not match the guarded read connection')


def reconcile(book, proof, now, apply=False):
    worker = proof['worker']
    digest = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    reason = 'verified_preauth_absent:' + digest[:24]
    with closing(sqlite3.connect(book.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        row = db.execute('SELECT * FROM remote_lease WHERE id=?', (worker['lease'],)).fetchone()
        require(row is not None, 'lease not found')
        if row['state'] == 'closed' and row['reason'] == reason:
            return {'status': 'already_closed', 'proof_sha256': digest}
        previous = db.execute('SELECT identity FROM remote_lease WHERE rowid < '
                              '(SELECT rowid FROM remote_lease WHERE id=?) AND node=? AND task=? '
                              'AND state=\'closed\' AND identity IS NOT NULL ORDER BY rowid DESC LIMIT 1',
                              (row['id'], row['node'], row['task'])).fetchone()
        require(previous is not None, 'no previous bound report session')
        validate(proof, row, json.loads(previous[0]), now)
        if apply:
            changed = db.execute("UPDATE remote_lease SET state='closed',reason=? WHERE id=? AND state='unknown' "
                                 "AND identity IS NULL AND reason='heartbeat_lost' AND heartbeat=?",
                                 (reason, row['id'], row['heartbeat'])).rowcount
            require(changed == 1, 'lease changed concurrently')
        db.execute('COMMIT')
    return {'status': 'closed' if apply else 'verified', 'lease': worker['lease'], 'proof_sha256': digest}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ledger', type=Path)
    parser.add_argument('proof', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    stat = args.proof.stat()
    require(os.geteuid() == stat.st_uid == 0 and not stat.st_mode & 0o077, 'root-private evidence required')
    print(json.dumps(reconcile(args.ledger.resolve(), json.loads(args.proof.read_bytes()), time.time(), args.apply)))
