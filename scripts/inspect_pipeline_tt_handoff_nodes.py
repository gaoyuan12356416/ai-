#!/usr/bin/env python3
"""Windows read-only SSH probes for a reviewed TT handoff plan.

Private node config: two nodes, each with node_id, host, python, identity_file.
Use an existing Python 3.9+ runtime. No keys or production hosts are in Git.
The captured <=60-second evidence is consumed by recover_pipeline_tt_selection.
"""
import argparse
import contextlib
import datetime
import inspect
import json
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_pipeline_tt_selection import document_hash, private_json, require


def probe_node(request):
    """Self-contained remote read-only probe; never returns payloads or env."""
    node = request['node_id']
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', node):
        raise RuntimeError('invalid_node_id')
    unit = 'pipeline-node-' + node + '.service'
    raw = subprocess.check_output(['systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'MainPID', '-p', 'EnvironmentFiles'], text=True)
    state = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    envpath = state.get('EnvironmentFiles', '').split(' ', 1)[0]
    if not envpath:
        raise RuntimeError('node_environment_unavailable')
    allowed = {}
    for line in Path(envpath).read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key in ('PIPELINE_NODE_ID', 'PIPELINE_NODE_STATE_ROOT', 'TT_POST_GPU_WORK_ROOT'):
            allowed[key] = json.loads(value)
    if allowed.get('PIPELINE_NODE_ID') != node:
        raise RuntimeError('node_identity_changed')
    root = Path(allowed['TT_POST_GPU_WORK_ROOT']); outbox = Path(allowed['PIPELINE_NODE_STATE_ROOT']) / 'receipts.sqlite3'
    matches = 0
    ids = {row['job_id'] for row in request['items']}
    with contextlib.closing(sqlite3.connect(outbox.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as c:
        c.execute('PRAGMA query_only=ON')
        for payload, in c.execute('SELECT payload FROM receipt_outbox'):
            if json.loads(payload).get('job_id') in ids:
                matches += 1
    files = []
    for item in request['items']:
        job, profile = item['gpu_job_id'], item['profile']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', job) or not re.fullmatch(r'[a-z0-9-]+', profile):
            raise RuntimeError('invalid_frozen_media_identity')
        for base in (root, root / 'profiles' / profile):
            for kind, target in (('manifest', base / 'manifests' / (job + '.json')),
                                 ('recipe', base / 'recipes' / (job + '.json')), ('scratch', base / 'jobs' / job)):
                if target.exists():
                    files.append({'job_id': item['job_id'], 'kind': kind})
    return {'node_id': node, 'unit': unit, 'active_state': state.get('ActiveState'), 'main_pid': int(state.get('MainPID', '-1')),
            'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'outbox_matching_count': matches,
            'task_file_matches': files, 'work_root_exists': root.is_dir(), 'outbox_exists': outbox.is_file()}


def inspect_nodes(plan, expected_sha, nodes, runner=subprocess.run):
    require(plan.get('plan_sha256') == expected_sha == document_hash(plan), 'plan_hash_mismatch')
    require(len(nodes) == 2 and sorted(row['node_id'] for row in nodes) == plan['node_ids'], 'node_config_scope')
    results = []
    for node in nodes:
        require(re.fullmatch(r'[A-Za-z0-9.-]+', node['host']) is not None, 'invalid_ssh_host')
        request = {'node_id': node['node_id'], 'items': [{key: row[key] for key in ('job_id', 'gpu_job_id', 'profile')} for row in plan['items']]}
        program = ('import contextlib,datetime,json,re,sqlite3,subprocess\nfrom pathlib import Path\n' + inspect.getsource(probe_node)
                   + '\nprint(json.dumps(probe_node(json.loads(' + repr(json.dumps(request)) + '))))\n')
        command = ['ssh', '-i', str(node['identity_file']), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                   '-o', 'StrictHostKeyChecking=yes', 'root@' + node['host'], shlex.quote(node['python']) + ' -']
        result = runner(command, input=program, text=True, capture_output=True, timeout=40)
        require(result.returncode == 0, 'node_probe_failed')
        row = json.loads(result.stdout)
        require(row.get('node_id') == node['node_id'], 'node_probe_identity_changed')
        results.append(row)
    return {'plan_sha256': expected_sha, 'nodes': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True); parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--node-config', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text()); nodes = json.loads(Path(args.node_config).read_text())['nodes']
    evidence = inspect_nodes(plan, args.plan_sha256, nodes)
    private_json(args.output, evidence)
    print(json.dumps({'ok': True, 'plan_sha256': evidence['plan_sha256'], 'nodes': [r['node_id'] for r in evidence['nodes']]}))


if __name__ == '__main__':
    main()
