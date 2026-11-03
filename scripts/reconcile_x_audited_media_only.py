#!/usr/bin/env python3
"""Exact nine-incident media-only reconciliation; no general unknown retry policy."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_prepost_recovery import digest
from scripts.recover_pipeline_x_preparation import RecoveryRejected, require
from scripts.x_media_only_reconciliation import AuditedMediaOnlyReconciler

QUEUE_IDS = (5485, 5487, 5501, 5508, 5532, 5534, 5522, 5523, 5459)
MANIFEST_PATH = Path(__file__).with_name('x_media_only_incidents_20261103.json')
MANIFEST_SHA = 'e51f5dddb50846216824c27e9cbb1e5c44f0fbbb5afcbcff31ff6b8fb5b19ff1'
AUDIT = 'pipeline_x_media_only_no_post_audit'
JOURNAL = 'pipeline_x_media_only_reconciliation_apply'
KIND = 'x_audited_media_only_no_post_20261103'


def incidents():
    data = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    require(data.get('version') == 1 and digest(data) == MANIFEST_SHA, 'fixed_incident_manifest_changed')
    require(sorted(i['QID'] for i in data['items']) == sorted(QUEUE_IDS), 'fixed_incident_scope_changed')
    return {i['QID']: i for i in data['items']}


def contract(queue_id):
    require(queue_id in QUEUE_IDS, 'queue_not_in_audited_incident_manifest')
    original = incidents()[queue_id]
    result = copy.deepcopy(original)
    require(result['PREP_GENERATION'] == 1 and result['ERROR_CODE'] in ('pipeline_lease_lost', 'coordinator_operation_failed'),
            'unreviewed_execution_shape')
    result.update(INCIDENT_SHA=digest(original), ERROR=result['ERROR_CODE'], PLAN_KIND=KIND,
        AUDIT=AUDIT, JOURNAL=JOURNAL, HOLD='operator_media_only_verified_prepost_pending',
        AUDIT_ERROR='audited_media_append_only', EVENT='audited_media_only_verified_no_post')
    return result


def engine_for(queue_id):
    return AuditedMediaOnlyReconciler(contract(queue_id))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'node-evidence', 'apply'))
    parser.add_argument('--queue-id', type=int, choices=QUEUE_IDS, required=True)
    for name in ('central', 'native', 'plan', 'node-id', 'outbox', 'backup-dir', 'actor'):
        parser.add_argument('--'+name)
    parser.add_argument('--node-evidence', nargs='+'); parser.add_argument('--output', required=True)
    args = parser.parse_args(); engine = engine_for(args.queue_id)
    with os.fdopen(os.open(args.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w', encoding='utf-8') as handle:
        if args.action == 'plan':
            result = engine.plan(args.central, args.native)
        else:
            saved = json.loads(Path(args.plan).read_text(encoding='utf-8'))
            if args.action == 'node-evidence':
                result = engine.node_evidence(saved, args.node_id, args.outbox)
            else:
                result = engine.apply(saved, [json.loads(Path(p).read_text(encoding='utf-8')) for p in args.node_evidence], args.backup_dir, args.actor)
        handle.write(json.dumps(result, ensure_ascii=False, indent=2)+'\n'); handle.flush(); os.fsync(handle.fileno())
    print(json.dumps({'ok': True, 'queue_id': args.queue_id, 'action': args.action, 'platform_called': False}))


if __name__ == '__main__':
    try: main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
