#!/usr/bin/env python3
"""Reconcile only audited q5524/generation 1, preserving its original unknown.

This is not an unknown retry policy. Exact historical worker, terminal ACK,
operation ledgers and frozen identity prove this one execution stopped before
Post. Admission uses the existing one-generation pre-Post runtime authority.
"""
import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.pipeline_dispatch.x_prepost_recovery import (
    TABLE as AUTH, SCHEMA as AUTH_SCHEMA, digest, encoded, queue_hash, log_hash, no_post_operation,
)
from scripts.recover_x_prepost_failures import backup, media_facts, stable_job, writable
from scripts.recover_pipeline_x_preparation import RecoveryRejected, require, read_connection, rows, date, document_hash, utcnow

QID, LOG_ID, PREP_ID, JOB_ID, ACCOUNT_ID, PARENT_ID = 5524, 5475, 31955, 31993, 43, 1563
KEY = 'x:queue:5524'
WORKER = 'hk-gpu-publish-0-ff381fa0'
WORKER_PID = 198472
RELEASE = 'ec8984773c4bc33e832ec5ae483cb3dcad7482dc'
RECEIPT_ID = 'b52ca6ec1895491f972ee928b9b952d5'
RECEIPT_SHA = '5bdb598791564bc592abc33d4e74e8eba3d3b7eed788b0d65c84d7ed12c95d8a'
# Original packets rechecked read-only on HK GPU. Hash both the original packet
# and its credential-redacted representation; operator files contain no bearer.
EXPECTED_RECEIPTS = {
    '979229e188d0cfb36ef2e73450b9ffa1573255dee18660e5b1c82f67ccac0ef6': ('jobs/checkpoint', 'e11dc3d57c9cde5d9a421f790c1cae898d278e652617c09b465521787296099d', '7e4c87714ff8bc44212667e9544e5c4f8b00cef7fc165168f447f20057667b1e'),
    'ec897e1c688a466f9bff2885ba641cce': ('jobs/complete', '917bd55da36cc5343edf3cb74407c2320547668e7c7b42b84e233a22b1892afc', '53367c6201e9b4c40d779d8a2fc4d1e1a99d6a9c3c0a30f60986bc4850e477fa'),
    '7eb75d61d0e4ea716105cb53acb7b5bbf50402939baabedf469bfa313995bc95': ('jobs/checkpoint', '407ae42c91049004d45d502463643954fe5850dd15479509118fcd160da47bd5', 'bd11bf0ebaa241ac17d3e1b34b548a0867a64679f7dbf256c7bc017b4c263dc2'),
    RECEIPT_ID: ('jobs/fail', RECEIPT_SHA, '70638fc0411c336ca1da1a05d41ef81fadadfdb0329eb16eb72ea55169474f9b'),
}
CLAIM_SHA = '27123d5069926ac14bfc4095b147771d9b1b460bd87fd8798e9441bfc2be982b'
HISTORY_DIR = '/data/pipeline-dispatch/maintenance/gpu-offload-20261103T090605Z'
RELEASE_ROOT = '/opt/pipeline-dispatch/releases/' + RELEASE
HISTORY_FILES = {
    'audit.json': '1fa01009a25a8be040ea0219efa481fb2163f977668e2d77ddcca44102e203a0',
    'node.service.before': '7ffe0db0e10edfd1dbb4652edebe2e5bde93e36428688dbdf21ef81187f80e7e',
}
RELEASE_FILES = {
    'features/pipeline_dispatch/worker.py': '7abc1300e57bc515a504d9295708b3d449e40c1ad9ba1dff6e6eb76a8e160b06',
    'features/pipeline_dispatch/executors.py': '0def164d2639687c3b7af3440de00693122fdf4fcfd0b999c0b88f1a05c2885f',
    'features/x_posts/service.py': '62bc80ea7b1b552b9e95744b8d5757611b04504979b20728be4b93a863d4a163',
}
EXPECTED = {
    'queue': '6966f39c0e5c722de8f56f494373523da1893325f2487c0d48bcab48648ee9b8',
    'log': '34205863744b8daa1e90a0663eb774496059d9ae68fb6d808ea4d238068dfd07',
    'native_ops': 'ce0cd105810bc63dcfaee647e4a3a351bb4b571c21d12abebc34f98f6390e878',
    'central_ops': '6e633ac2c5955be7b281068fad952e693ae418b252f5f19f85e7c1276db69e5f',
    'history': 'a8228525f12ee2fdf733f807ee27d4afd9caba4644d6acfb095becc985cc03f9',
    'claims': 'de76a2158082694113b9db23b6a6ba98becab04998e67f77be798435b0483658',
}
HOLD = 'operator_5524_verified_prepost_pending'
AUDIT = 'pipeline_x_5524_no_post_audit'
JOURNAL = 'pipeline_x_5524_reconciliation_apply'
AUDIT_SCHEMA = 'CREATE TABLE IF NOT EXISTS '+AUDIT+' (job_id INTEGER PRIMARY KEY,plan_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)'
JOURNAL_SCHEMA = 'CREATE TABLE IF NOT EXISTS '+JOURNAL+' (job_id INTEGER PRIMARY KEY,plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,backups_json TEXT NOT NULL,updated_at TEXT NOT NULL)'



from scripts.x_media_only_reconciliation import AuditedMediaOnlyReconciler, one, exists, comparable, file_hash


def _engine():
    # Keep the original q5524 plan/table/receipt format and all existing hashes.
    fields = ['ACCOUNT_ID', 'AUDIT', 'CLAIM_SHA', 'EXPECTED', 'HISTORY_DIR', 'HISTORY_FILES', 'HOLD', 'JOB_ID', 'JOURNAL', 'KEY', 'LOG_ID', 'PARENT_ID', 'PREP_ID', 'QID', 'RECEIPT_ID', 'RECEIPT_SHA', 'RELEASE', 'RELEASE_FILES', 'RELEASE_ROOT', 'WORKER', 'WORKER_PID']
    incident = {name: globals()[name] for name in fields}
    incident.update(PUBLISH_NODE='hk-gpu', OP_COUNT=83, ERROR='pipeline_lease_lost',
        PLAN_KIND='x_5524_verified_no_post', AUDIT_ERROR='5524_audit_append_only',
        EVENT='audited_5524_verified_no_post', INCIDENT_SHA='',
        RECEIPTS_BY_NODE={'cpu-main': {}, 'hk-gpu': EXPECTED_RECEIPTS})
    return AuditedMediaOnlyReconciler(incident, clock=utcnow)


def collect(c, n):
    return _engine().collect(c, n)


def initial_proof(c, n, now):
    return _engine().initial_proof(c, n, now)


def plan(central, native, now=None):
    return _engine().plan(central, native, now)


def validate_plan(saved):
    return _engine().validate_plan(saved)


def historical_worker(history_dir=HISTORY_DIR, release_root=RELEASE_ROOT):
    return _engine().historical_worker(history_dir, release_root)


def node_evidence(saved, node_id, outbox, now=None, *, history_dir=HISTORY_DIR, release_root=RELEASE_ROOT):
    return _engine().node_evidence(saved, node_id, outbox, now, history_dir=history_dir, release_root=release_root)


def validate_nodes(saved, evidence, now):
    return _engine().validate_nodes(saved, evidence, now)


def assert_state(saved, c, n, native_armed=False):
    return _engine().assert_state(saved, c, n, native_armed)


def current_authorization(saved, n, *, allow_missing=False):
    return _engine().current_authorization(saved, n, allow_missing=allow_missing)


def append_audit(c, saved, evidence, backups, actor, now):
    return _engine().append_audit(c, saved, evidence, backups, actor, now)


def apply(saved, evidence, backup_dir, actor, now=None, stop_after=None):
    return _engine().apply(saved, evidence, backup_dir, actor, now, stop_after)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'node-evidence', 'apply'))
    for name in ('central', 'native', 'plan', 'node-id', 'outbox', 'backup-dir', 'actor'):
        parser.add_argument('--'+name)
    parser.add_argument('--node-evidence', nargs='+'); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with os.fdopen(os.open(args.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w', encoding='utf-8') as handle:
        if args.action == 'plan': result = plan(args.central, args.native)
        else:
            saved = json.loads(Path(args.plan).read_text(encoding='utf-8'))
            if args.action == 'node-evidence': result = node_evidence(saved, args.node_id, args.outbox)
            else: result = apply(saved, [json.loads(Path(x).read_text(encoding='utf-8')) for x in args.node_evidence], args.backup_dir, args.actor)
        handle.write(json.dumps(result, ensure_ascii=False, indent=2)+'\n'); handle.flush(); os.fsync(handle.fileno())
    print(json.dumps({'ok': True, 'action': args.action, 'queue_id': QID, 'platform_called': False}))


if __name__ == '__main__':
    try: main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
