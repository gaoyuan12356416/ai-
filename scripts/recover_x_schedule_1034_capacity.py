#!/usr/bin/env python3
"""One fourth admission of 1034 after its exact pre-transaction HTTP 409.

This is not a generic empty-plan retry. The previous plan-attempt fence and
all three recovery audits are preserved in a separate immutable audit. The
normal runner alone may reverify accounts, select the original pool and plan.
"""
import argparse
import ast
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_schedule_recovery import identity, fingerprint
from scripts.recover_pipeline_x_preparation import RecoveryRejected, read_connection, require, rows
from scripts.recover_x_prepost_failures import backup, writable
from scripts.recover_x_schedule_read_preflight import audit, verify_deployed

RUN_ID, PREDECESSOR = 1034, 1036
TABLE = 'x_post_schedule_1034_capacity_resume_audit'
FIRST = 'x_post_schedule_zero_write_resume_audit'
SECOND = 'x_post_schedule_scope_churn_resume_audit'
THIRD = 'x_post_schedule_read_preflight_resume_audit'
ERROR = 'x_post_pool_fifo_conflict'
MESSAGE = '素材语言尚未达到冻结批次容量'
FAILED_AT = '2026-11-03T13:05:17Z'
HISTORICAL_COMMIT = '179596a9699393cd377c0e06a0bec94e171f14cd'
FUNCTION_SHA256 = '87f919add7b7fd796ee9f5f804b61c19fec500de555f388ce7c5a03c1185b3ec'
ANCHOR = dict(
    run_sha256='4635dd05bf83fe93b7b3a83762644366c16f70f5b8c2f9d7128d979be2cdd838',
    identity_sha256='5c33a45241b45be3552f6b42c9df18e4f7c8f8c65065c2d070039fe01c493a41',
    failure_sha256='c09b4b5c103b8207245debe822939822da4a9d16a11ff3d79751f3765c87d90b',
    audits={FIRST: '13f5bb4717fcaca7d2d90d3c2d1eab53b00ab17c5a5f9a4e0853767533d112c5',
            SECOND: 'ca1aa5392a20cbbd04d8cb996b756fa5b5bb35fb1107e6f4fb429be889d1c914',
            THIRD: 'fc0f8d57d318f83a3d5d7d3aea6802a4ec54fbb94eaa388cfda570ceb14ae6e7'},
    predecessor_identity_sha256='e9f43f74215715c813ff5eb7f9937e893d1bba7fb2a125adde355e0754cd3fbc',
    predecessor_queues_sha256='0cf64116931834b1207656e6526e39091dd80558f542bfb478c05823c13e3005',
)
QUEUE_FIELDS = ('id', 'idempotency_key', 'account_id', 'material_key', 'pool_item_id',
                'schedule_run_id', 'created_at')
SCHEMA = '''CREATE TABLE IF NOT EXISTS x_post_schedule_1034_capacity_resume_audit (
 run_id INTEGER PRIMARY KEY CHECK(run_id=1034),failed_at TEXT NOT NULL,
 previous_plan_attempted_at TEXT NOT NULL,identity_sha256 TEXT NOT NULL,
 plan_sha256 TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,
 authorized_at TEXT NOT NULL,backup_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL)'''


def failure_identity(run):
    return {k: run[k] for k in ('error_code', 'error_message', 'finished_at',
                               'updated_at', 'plan_attempted_at')}


def historical_rejection(repository):
    result = subprocess.run(['git', '-C', str(repository), 'show',
                             HISTORICAL_COMMIT + ':features/x_posts/service.py'],
                            capture_output=True, timeout=15, check=False)
    require(result.returncode == 0, 'historical_service_unavailable')
    tree = ast.parse(result.stdout)
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name == 'create_schedule_plan']
    require(len(functions) == 1, 'historical_plan_function_changed')
    function = functions[0]
    lines = result.stdout.splitlines(keepends=True)
    sha = hashlib.sha256(b''.join(lines[function.lineno-1:function.end_lineno])).hexdigest()
    require(sha == FUNCTION_SHA256, 'historical_plan_function_changed')
    guards = [n for n in ast.walk(function) if isinstance(n, ast.Raise)
              and isinstance(n.exc, ast.Call) and len(n.exc.args) == 3
              and all(isinstance(arg, ast.Constant) for arg in n.exc.args)
              and [arg.value for arg in n.exc.args] == [ERROR, MESSAGE, 409]]
    begins = [n.lineno for n in ast.walk(function)
              if isinstance(n, ast.Constant) and n.value == 'BEGIN IMMEDIATE']
    require(len(guards) == 1 and begins and guards[0].lineno < min(begins),
            'not_exact_pretransaction_rejection')
    return dict(commit=HISTORICAL_COMMIT, function_sha256=sha,
                rejection_line=guards[0].lineno, first_transaction_line=min(begins),
                code=ERROR, message=MESSAGE, http_status=409, transaction_started=False)


def verify_code(commit, runner_path, repository):
    proof = verify_deployed(commit, runner_path, repository)
    root = Path(runner_path).absolute().parent.parent
    require(b'material_loader_options["allow_images"]' in (root/'scripts/x_post_schedule_runner.py').read_bytes()
            and b'allow_images=allow_images' in (root/'features/x_posts/selector.py').read_bytes(),
            'random_video_selection_fix_missing')
    return dict(deployed=proof, historical_rejection=historical_rejection(repository))


def verify_deployed_hashes(proof):
    """No subprocess or remote request while holding the native write lock."""
    deployed = proof['deployed']; path = Path(deployed['runner_path'])
    require(str(path.resolve()) == deployed['runner_resolved_path'], 'runtime_path_changed')
    root = path.parent.parent
    for name, sha in deployed['files'].items():
        candidate = root/name
        require(candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == sha,
                'deployed_repair_changed')


def snapshot(c):
    found = rows(c, 'SELECT * FROM x_post_schedule_run WHERE id=?', (RUN_ID,))
    require(len(found) == 1, 'original_run_missing'); run = found[0]
    require(run['status'] == 'failed_preflight' and run['error_code'] == ERROR
            and run['error_message'] == MESSAGE
            and run['finished_at'] == run['updated_at'] == run['plan_attempted_at'] == FAILED_AT
            and run['source_type'] == 'material' and run['run_date'] == '2026-11-03'
            and run['publish_time'] == '08:45' and run['config_version'] == 33
            and run['material_pool_snapshot_max_id'] == 4444
            and run['expected_count'] == len(set(json.loads(run['account_ids_json']))) == 89
            and not any(run[k] for k in ('started_at', 'queued_count', 'published_count',
                                         'failed_count', 'unknown_count')),
            'not_exact_pretransaction_capacity_failure')
    require(fingerprint(run) == ANCHOR['run_sha256']
            and fingerprint(identity(run)) == ANCHOR['identity_sha256']
            and fingerprint(failure_identity(run)) == ANCHOR['failure_sha256'], 'original_failure_or_identity_changed')
    require(not c.execute('SELECT 1 FROM x_post_queue WHERE schedule_run_id=?', (RUN_ID,)).fetchone(),
            'original_run_has_queue')
    require(not audit(c, TABLE, RUN_ID), 'fourth_admission_exists')
    previous = {}
    for table, sha in ANCHOR['audits'].items():
        found = audit(c, table, RUN_ID)
        require(len(found) == 1 and fingerprint(found[0]) == sha, 'previous_audit_changed')
        previous[table] = found[0]
    require(previous[FIRST]['identity_sha256'] == ANCHOR['identity_sha256'],
            'original_authorization_identity_changed')
    require(not audit(c, 'x_post_schedule_episode_filter_retry_audit', RUN_ID), 'unexpected_previous_audit')
    earlier = rows(c, 'SELECT * FROM x_post_schedule_run WHERE id=?', (PREDECESSOR,))
    require(len(earlier) == 1, 'preceding_plan_missing'); earlier = earlier[0]
    require(earlier['status'] not in ('claimed', 'failed_preflight')
            and earlier['started_at'] == earlier['plan_attempted_at'] == '2026-11-03T13:31:33Z'
            and earlier['queued_count'] == earlier['expected_count'] == 42
            and earlier['material_pool_snapshot_max_id'] == 4444
            and fingerprint(identity(earlier)) == ANCHOR['predecessor_identity_sha256'],
            'preceding_plan_not_committed')
    queues = rows(c, 'SELECT '+','.join(QUEUE_FIELDS)+' FROM x_post_queue WHERE schedule_run_id=? ORDER BY id',
                  (PREDECESSOR,))
    require([q['id'] for q in queues] == list(range(5667, 5709))
            and fingerprint(queues) == ANCHOR['predecessor_queues_sha256'], 'preceding_queue_identity_changed')
    return dict(run_id=RUN_ID, previous_run=run, previous_audits=previous,
                identity_sha256=ANCHOR['identity_sha256'], queue_count=0,
                preceding_plan=dict(run_id=PREDECESSOR, identity=identity(earlier),
                                    plan_attempted_at=earlier['plan_attempted_at'], queues=queues))


def plan(path, commit, runner_path, repository):
    proof = verify_code(commit, runner_path, repository)
    with contextlib.closing(read_connection(path)) as c:
        c.execute('BEGIN')
        result = dict(kind='x_schedule_1034_exact_pretransaction_capacity_resume', version=1,
                      db_path=str(Path(path).resolve()), run_id=RUN_ID, item=snapshot(c), code_evidence=proof)
    return dict(result, plan_sha256=fingerprint(result))


def existing(c, saved):
    found = audit(c, TABLE, RUN_ID)
    if not found: return False
    require(len(found) == 1 and found[0]['plan_sha256'] == saved['plan_sha256'], 'different_fourth_admission_exists')
    return True


def install(c):
    c.execute(SCHEMA)
    for operation in ('UPDATE', 'DELETE'):
        c.execute('CREATE TRIGGER IF NOT EXISTS '+TABLE+'_no_'+operation.lower()+' BEFORE '+operation+
                  ' ON '+TABLE+" BEGIN SELECT RAISE(ABORT,'capacity_recovery_audit_immutable'); END")
    c.execute('CREATE TRIGGER IF NOT EXISTS '+TABLE+'_no_replace BEFORE INSERT ON '+TABLE+
              ' WHEN EXISTS(SELECT 1 FROM '+TABLE+' WHERE run_id=NEW.run_id)'+
              " BEGIN SELECT RAISE(ABORT,'capacity_recovery_audit_immutable'); END")


def apply(path, saved, backup_dir, actor, reason):
    require(saved.get('kind') == 'x_schedule_1034_exact_pretransaction_capacity_resume'
            and saved.get('version') == 1 and saved.get('run_id') == RUN_ID
            and saved['plan_sha256'] == fingerprint({k:v for k,v in saved.items() if k != 'plan_sha256'}),
            'plan_invalid')
    require(saved['db_path'] == str(Path(path).resolve()) and str(actor).strip() and str(reason).strip(),
            'path_actor_reason_invalid')
    proof = saved['code_evidence']; deployed = proof['deployed']
    with contextlib.closing(read_connection(path)) as c:
        if existing(c, saved):
            return dict(already_applied=True, run_id=RUN_ID, plan_sha256=saved['plan_sha256'], platform_called=False)
        c.execute('BEGIN'); require(snapshot(c) == saved['item'], 'plan_state_drift')
    require(verify_code(deployed['commit'], deployed['runner_path'], deployed['repository']) == proof,
            'code_evidence_changed')
    target = backup(path, backup_dir, 'schedule-1034-fourth'); sha = hashlib.sha256()
    with open(target, 'rb') as stream:
        for data in iter(lambda:stream.read(1024*1024), b''): sha.update(data)
    with contextlib.closing(writable(path)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            if existing(c, saved):
                c.rollback()
                return dict(already_applied=True, run_id=RUN_ID, plan_sha256=saved['plan_sha256'], platform_called=False)
            verify_deployed_hashes(proof); require(snapshot(c) == saved['item'], 'plan_state_drift')
            install(c)
            now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            evidence = dict(saved['item'], code_evidence=proof, backup_path=target,
                            prior_plan_attempt_retained=True, transaction_started=False)
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (RUN_ID, FAILED_AT, saved['item']['previous_run']['plan_attempted_at'],
                       ANCHOR['identity_sha256'], saved['plan_sha256'], actor, reason, now,
                       sha.hexdigest(), json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
            changed = c.execute("UPDATE x_post_schedule_run SET status='claimed',error_code='',error_message='',"
                "finished_at='',plan_attempted_at='',updated_at=?,lease_heartbeat_at=? WHERE id=? "
                "AND status='failed_preflight' AND plan_attempted_at=? AND finished_at=? AND error_code=?",
                (now, now, RUN_ID, FAILED_AT, FAILED_AT, ERROR)).rowcount
            require(changed == 1, 'schedule_compare_failed'); c.commit()
        except BaseException:
            c.rollback(); raise
    return dict(applied=True, run_id=RUN_ID, plan_sha256=saved['plan_sha256'], backup_path=target,
                backup_sha256=sha.hexdigest(), platform_called=False, queues_created=0,
                previous_audits_modified=False, previous_plan_attempted_at=FAILED_AT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply'))
    for name in ('db', 'repair-commit', 'runner-path', 'repository', 'plan', 'backup-dir', 'actor', 'reason'):
        parser.add_argument('--'+name)
    parser.add_argument('--output', required=True); args = parser.parse_args()
    with os.fdopen(os.open(args.output, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600), 'w', encoding='utf-8') as stream:
        result = (plan(args.db, args.repair_commit, args.runner_path, args.repository) if args.action == 'plan'
                  else apply(args.db, json.loads(Path(args.plan).read_text(encoding='utf-8')),
                             args.backup_dir, args.actor, args.reason))
        json.dump(result, stream, ensure_ascii=False, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    print(json.dumps({k:result[k] for k in ('run_id', 'plan_sha256', 'applied', 'already_applied') if k in result}))


if __name__ == '__main__':
    main()
