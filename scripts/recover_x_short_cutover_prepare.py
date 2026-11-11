"""Recover an exact zero-write preparation rejected during sidecar cutover.

Reuse the deployed audited recovery utility and all its ledger, claim, outbox,
CAS and backup checks. The extra coordinator error is accepted only in prepare
mode, where zero operations and zero publication attempts are mandatory.
"""
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['plan', 'node-evidence', 'apply'])
    p.add_argument('--recovery-source', required=True)
    p.add_argument('--runtime', default='/opt/x-post-automation/current')
    p.add_argument('--central'); p.add_argument('--native')
    p.add_argument('--queue-id', type=int); p.add_argument('--day')
    p.add_argument('--plan'); p.add_argument('--node-id'); p.add_argument('--outbox')
    p.add_argument('--node-evidence', nargs='+'); p.add_argument('--backup-dir')
    p.add_argument('--output', required=True)
    args = p.parse_args()
    # Use the active native store for parent aggregation. The recovery utility
    # itself comes from an already versioned source; no live source is edited.
    sys.path.insert(0, args.runtime)
    from features.x_posts import service
    sys.path.insert(0, args.recovery_source)
    from scripts import recover_x_prepost_failures as recovery
    recovery.KNOWN_CODES = recovery.KNOWN_CODES | {'coordinator_operation_failed'}
    if args.action == 'plan':
        result = recovery.make_plan(args.central, args.native, [args.queue_id], 'prepare', [args.day])
        assert all(i['prior_job']['error_code'] == 'coordinator_operation_failed'
                   and i['prior_attempts'] == 0 for i in result['items'])
    else:
        plan = json.loads(Path(args.plan).read_text())
        assert plan['stage'] == 'prepare' and len(plan['items']) == 1
        assert all(i['prior_job']['error_code'] == 'coordinator_operation_failed'
                   and i['prior_attempts'] == 0 for i in plan['items'])
        if args.action == 'node-evidence':
            result = recovery.node_evidence(plan, args.node_id, args.outbox)
        else:
            # Reserve output before apply, so a receipt cannot be lost because
            # an existing evidence file would otherwise be overwritten.
            with os.fdopen(os.open(args.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w') as f:
                result = recovery.apply_plan(plan,
                    [json.loads(Path(x).read_text()) for x in args.node_evidence], args.backup_dir,
                    'codex:repair-short-link-deployment-zero-write-preparation')
                json.dump(result, f, indent=2); f.flush(); os.fsync(f.fileno())
            print(json.dumps(result)); return
    with os.fdopen(os.open(args.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w') as f:
        json.dump(result, f, indent=2); f.flush(); os.fsync(f.fileno())
    print(json.dumps(dict(action=args.action, plan_sha256=result['plan_sha256'])))


if __name__ == '__main__':
    main()
