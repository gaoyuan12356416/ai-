#!/usr/bin/env python3
"""One admission of slot 1031 after the exact preceding 19 queues settle.

plan and wait-plan are read-only. apply admits the original slot once; only the
normal runner may preflight and create its queues. This never advances episodes.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_schedule_recovery import fingerprint, identity
from features.x_posts import service
from features.x_posts.account_blockers import read_account_publish_blockers
from scripts.recover_pipeline_x_preparation import RecoveryRejected, read_connection, require, rows
from scripts.recover_x_prepost_failures import backup, writable
from scripts.recover_x_schedule_read_preflight import audit

RUN_ID = 1031
DEPENDENCIES = list(range(5633, 5652))
FIRST = 'x_post_schedule_zero_write_resume_audit'
SECOND = 'x_post_schedule_episode_filter_retry_audit'
TABLE = 'x_post_schedule_1031_shortage_resume_audit'
ERROR = 'x_post_schedule_drama_shortage'
MESSAGE = '当前账号没有可续播的空闲剧集，请检查已绑定剧集的失败记录或同语言库存'
ANCHOR = dict(
    failed_at='2026-11-03T12:50:31Z',
    run_sha256='df7a6a4176b7ba0d3942f4781805e4c60eecf8e66d8f4f86628f20f7b4299052',
    identity_sha256='a364aea4261cc7d0285aec2f8eea1400cbfe27b78dfcdf83aeade386f981baa9',
    audits={FIRST: 'cb7bdd8e35ab0fc306e6a8524245f405a3be536f86372d28e97a329600979f1c',
            SECOND: '79fa67a4a2b1aa4e72db11f088d6073c341edb79f9f26849fbada788b81dc968'},
    dependencies_sha256='3ac074f287a6b13d67a5fa45b252782f0027b3fc66915bdebe3450a59e4b3ff8')
QUEUE_IDENTITY = ('id', 'account_id', 'run_date', 'schedule_run_id', 'content_id',
    'episode_number', 'episode_key', 'drama_pool_item_id', 'drama_replay_generation',
    'body_template', 'random_template_enabled', 'created_at')
CODE_FILES = ('features/x_posts/service.py', 'features/x_posts/account_blockers.py',
              'scripts/x_post_schedule_runner.py')
SCHEMA = '''CREATE TABLE IF NOT EXISTS x_post_schedule_1031_shortage_resume_audit (
 run_id INTEGER PRIMARY KEY CHECK(run_id=1031),plan_sha256 TEXT NOT NULL,
 identity_sha256 TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,
 authorized_at TEXT NOT NULL,backup_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL)'''


def verify_code(commit, service_path, repository):
    require(isinstance(commit, str) and re.fullmatch(r'[a-f0-9]{40}', commit), 'full_commit_required')
    root = Path(service_path).absolute().parent.parent.parent
    files = {}
    for name in CODE_FILES:
        result = subprocess.run(['git', '-C', str(repository), 'show', commit + ':' + name],
                                capture_output=True, timeout=15, check=False)
        require(result.returncode == 0, 'committed_code_missing')
        target = root / name
        require(target.is_file() and target.read_bytes() == result.stdout, 'deployed_code_drift')
        # The read-only local selector used for this proof must be the deployed one.
        if name != CODE_FILES[-1]:
            require((Path(__file__).resolve().parents[1] / name).read_bytes() == result.stdout,
                    'local_selector_differs_from_deployed')
        files[name] = hashlib.sha256(result.stdout).hexdigest()
    require(b'"x_post_schedule_drama_shortage"' in result.stdout, 'shortage_guard_missing')
    return dict(commit=commit, service_path=str(Path(service_path).absolute()),
                service_resolved_path=str(Path(service_path).resolve()),
                repository=str(Path(repository).absolute()), files=files)


def verify_file_hashes(proof):
    """Fast final check: no subprocess while holding the native write lock."""
    service_path = Path(proof['service_path'])
    require(str(service_path.resolve()) == proof['service_resolved_path'], 'deployed_code_drift')
    root = service_path.parent.parent.parent
    for name, expected in proof['files'].items():
        path = root / name
        require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'deployed_code_drift')
        if name != CODE_FILES[-1]:
            require(hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest() == expected,
                    'local_selector_differs_from_deployed')


def original_failure(n):
    found = rows(n, 'SELECT * FROM x_post_schedule_run WHERE id=?', (RUN_ID,))
    require(len(found) == 1, 'original_slot_missing')
    run = found[0]
    require(run['status'] == 'failed_preflight' and run['error_code'] == ERROR
            and run['error_message'] == MESSAGE
            and run['finished_at'] == run['updated_at'] == ANCHOR['failed_at']
            and run['source_type'] == 'drama' and run['run_date'] == '2026-11-03'
            and run['material_pool_snapshot_max_id'] is None
            and not any(run[k] for k in ('started_at', 'plan_attempted_at', 'queued_count',
                                         'published_count', 'failed_count', 'unknown_count')),
            'not_exact_shortage_failure')
    require(fingerprint(run) == ANCHOR['run_sha256']
            and fingerprint(identity(run)) == ANCHOR['identity_sha256'], 'original_failure_or_scope_drift')
    require(not n.execute('SELECT 1 FROM x_post_queue WHERE schedule_run_id=?', (RUN_ID,)).fetchone(),
            'original_slot_has_queue')
    require(not audit(n, TABLE, RUN_ID), 'third_admission_already_recorded')
    previous = {}
    for table, expected in ANCHOR['audits'].items():
        found = audit(n, table, RUN_ID)
        require(len(found) == 1 and fingerprint(found[0]) == expected, 'previous_audit_drift')
        previous[table] = found[0]
    require(previous[FIRST]['identity_sha256'] == ANCHOR['identity_sha256'], 'first_authorization_scope_drift')
    return dict(previous_run=run, previous_audits=previous,
                identity_sha256=ANCHOR['identity_sha256'], queue_count=0, log_count=0)


def post_operation(value):
    return value == 'x:POST:/2/tweets' or bool(re.fullmatch(r'x:POST:/2/users/[^/]+/retweets', value))


def dependencies(n, c):
    frozen = rows(n, 'SELECT ' + ','.join(QUEUE_IDENTITY) +
                  ' FROM x_post_queue WHERE schedule_run_id=1029 ORDER BY id')
    require([r['id'] for r in frozen] == DEPENDENCIES
            and fingerprint(frozen) == ANCHOR['dependencies_sha256'], 'dependency_frozen_identity_drift')
    result = []
    for frozen_queue in frozen:
        qid = frozen_queue['id']; key = 'x:queue:' + str(qid)
        queue = rows(n, 'SELECT id,status,relay_account_id FROM x_post_queue WHERE id=?', (qid,))[0]
        logs = rows(n, 'SELECT id,queue_id,account_id,status,attempt_count,unknown_outcome,'
                      'x_post_id,published_at,error_code,updated_at FROM x_post_publish_log WHERE queue_id=?', (qid,))
        relay = rows(n, 'SELECT status,unknown_outcome,source_post_id,repost_id,source_published_at,reposted_at '
                       'FROM x_post_repost_ledger WHERE queue_id=?', (qid,))
        jobs = rows(c, 'SELECT id,source_key,stage,status,generation,hold_reason,lease_expires_at,'
                      'external_intent,error_code,updated_at FROM pipeline_job WHERE source_key=? ORDER BY stage', (key,))
        resources = rows(c, 'SELECT r.resource_key,r.job_id,r.generation,r.quarantined FROM pipeline_resource r '
                           'JOIN pipeline_job j ON j.id=r.job_id WHERE j.source_key=? ORDER BY r.resource_key', (key,))
        legacy_resources = rows(c, 'SELECT r.resource_key,r.job_id FROM pipeline_legacy_resource r '
                                  'JOIN pipeline_job j ON j.id=r.job_id WHERE j.source_key=? ORDER BY r.resource_key', (key,))
        publication = rows(c, 'SELECT source_key,job_id,publish_id,published_at FROM pipeline_publication '
                              'WHERE source_key=?', (key,))
        native_ops = rows(n, 'SELECT operation_id,generation,operation,request_sha256 FROM pipeline_x_operation '
                            'WHERE source_key=? ORDER BY operation_id', (key,))
        central_ops = rows(c, 'SELECT o.operation_id,o.generation,o.operation,o.request_sha256 FROM pipeline_operation o '
                             'JOIN pipeline_job j ON j.id=o.job_id WHERE j.source_key=? ORDER BY o.operation_id', (key,))
        reason = ''
        if queue['status'] not in ('published', 'failed'):
            reason = 'dependency_not_terminal'
        elif len(logs) != 1 or logs[0]['unknown_outcome'] or any(r['unknown_outcome'] for r in relay):
            reason = 'dependency_unknown_or_receipt_missing'
        elif not jobs or any(j['status'] not in ('completed', 'published', 'failed') or j['lease_expires_at'] for j in jobs):
            reason = 'dependency_central_not_terminal'
        elif resources or legacy_resources:
            reason = 'dependency_execution_resource_remains'
        elif native_ops != central_ops:
            reason = 'dependency_operation_ledger_delta'
        elif queue['status'] == 'published':
            pubs = [j for j in jobs if j['stage'] == 'publish' and j['status'] == 'published']
            log = logs[0]
            if (log['status'] != 'published' or not log['x_post_id'] or not log['published_at']
                    or len(pubs) != 1 or len(publication) != 1
                    or publication[0]['job_id'] != pubs[0]['id']
                    or publication[0]['publish_id'] != log['x_post_id']
                    or not publication[0]['published_at']
                    or (queue['relay_account_id'] and (len(relay) != 1 or relay[0]['status'] != 'reposted'
                         or not relay[0]['source_post_id'] or not relay[0]['reposted_at']))):
                reason = 'dependency_success_receipt_mismatch'
        elif (logs[0]['status'] != 'failed' or logs[0]['x_post_id'] or logs[0]['published_at'] or publication
                or not any(j['status'] == 'failed' for j in jobs)
                or any(j['status'] == 'published' for j in jobs)
                or any(post_operation(o['operation']) for o in native_ops)
                or any(r['source_post_id'] or r['repost_id'] or r['status'] != 'failed' for r in relay)):
            # A potentially accepted Post is never inferred safe from unknown=0.
            # Such failures need a separate receipt review, not this admission.
            reason = 'dependency_failure_not_proven_prepost'
        result.append(dict(queue=frozen_queue, state=queue, logs=logs, relay=relay, jobs=jobs,
            resources=resources, legacy_resources=legacy_resources, publication=publication,
            operation_count=len(native_ops), native_operations_sha256=fingerprint(native_ops),
            central_operations_sha256=fingerprint(central_ops), wait_reason=reason))
    return result


def candidates(n, run):
    ids = json.loads(run['account_ids_json']); marks = ','.join('?' for _ in ids)
    accounts = rows(n, 'SELECT id,status,publish_approved,drama_language FROM x_authorized_account '
                      'WHERE id IN (' + marks + ') ORDER BY id', ids)
    require({r['id'] for r in accounts} == set(ids), 'frozen_account_missing')
    blockers = read_account_publish_blockers(n, ids)
    assignments = service.XPostStore._drama_assignment_candidates(n, ids, 1000,
        account_languages={r['id']: r['drama_language'] for r in accounts}, configured_account_ids=ids)
    selected = {r['candidate_account_id']: r for r in assignments}
    reasons = []
    for aid in ids:
        bound = rows(n, "SELECT id,content_id,status,next_sub_number,free_episode_count,replay_generation,"
                        "last_error_code,language FROM x_post_drama_pool WHERE assigned_account_id=? "
                        "AND status IN ('pending','active') AND free_episode_count>0 "
                        "AND next_sub_number<=free_episode_count ORDER BY id", (aid,))
        occupied = []
        for pool in bound:
            occupied.extend(rows(n, 'SELECT id,status,schedule_run_id FROM x_post_queue WHERE episode_key=? ORDER BY id',
                (service._drama_episode_key(pool['content_id'], pool['next_sub_number'], pool['replay_generation']),)))
        reason = ('candidate_available' if aid in selected else
                  blockers[aid]['code'] if aid in blockers else
                  'bound_drama_validation_failure' if any(r['last_error_code'] for r in bound) else
                  'bound_current_episode_occupied' if occupied else 'no_eligible_same_language_pool')
        blocker = {k: v for k, v in blockers.get(aid, {}).items() if k not in ('message', 'account_username')}
        reasons.append(dict(account_id=aid, reason=reason, blocker=blocker, bound_pool=bound, occupied_queues=occupied))
    return dict(accounts=accounts, candidates=assignments, account_reasons=reasons,
                normal_source_account_media_preflight_required=True)


def snapshot(n, c):
    original = original_failure(n)
    deps = dependencies(n, c)
    pending = [r['queue']['id'] for r in deps if r['wait_reason']]
    result = dict(original=original, dependencies=deps, pending_queue_ids=pending)
    if pending:
        result.update(state='waiting_dependencies', eligibility=None)
    else:
        eligibility = candidates(n, original['previous_run'])
        require(eligibility['candidates'], 'no_fresh_candidates_after_dependencies')
        result.update(state='ready', eligibility=eligibility)
    return result


def plan(native, central, commit, service_path, repository):
    proof = verify_code(commit, service_path, repository)
    with contextlib.closing(read_connection(native)) as n, contextlib.closing(read_connection(central)) as c:
        n.execute('BEGIN'); c.execute('BEGIN')
        result = dict(kind='x_schedule_1031_shortage_once', version=1, run_id=RUN_ID,
            native=str(Path(native).resolve()), central=str(Path(central).resolve()),
            code_evidence=proof, snapshot=snapshot(n, c))
    result['plan_sha256'] = fingerprint(result)
    return result


def apply(saved, expected_sha, backup_dir, actor, reason):
    require(saved.get('kind') == 'x_schedule_1031_shortage_once' and saved.get('version') == 1
            and saved.get('run_id') == RUN_ID and saved.get('plan_sha256') == expected_sha
            == fingerprint({k: v for k, v in saved.items() if k != 'plan_sha256'}), 'plan_hash_mismatch')
    require(saved['snapshot']['state'] == 'ready' and str(actor).strip() and str(reason).strip(), 'ready_plan_actor_reason_required')
    native, central, proof = saved['native'], saved['central'], saved['code_evidence']

    def check(n, c, inside_transaction=False):
        if inside_transaction:
            verify_file_hashes(proof)
        else:
            require(verify_code(proof['commit'], proof['service_path'], proof['repository']) == proof, 'code_evidence_drift')
        require(snapshot(n, c) == saved['snapshot'], 'plan_state_drift')

    def already(n):
        existing = audit(n, TABLE, RUN_ID)
        if existing:
            require(len(existing) == 1 and existing[0]['plan_sha256'] == expected_sha, 'different_admission_already_recorded')
        return bool(existing)

    result = dict(run_id=RUN_ID, plan_sha256=expected_sha, platform_called=False, queues_created=0)
    with contextlib.closing(read_connection(native)) as n, contextlib.closing(read_connection(central)) as c:
        if already(n):
            return dict(result, already_applied=True)
        n.execute('BEGIN'); c.execute('BEGIN'); check(n, c)
    target = backup(native, backup_dir, 'schedule-1031-shortage')
    digest = hashlib.sha256()
    with open(target, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
    with contextlib.closing(writable(native)) as n, contextlib.closing(read_connection(central)) as c:
        n.execute('BEGIN IMMEDIATE')
        try:
            if already(n):
                n.rollback(); return dict(result, already_applied=True)
            # Keep the central reads fresh while the native write lock fences
            # native permits/receipts; no central mutation or global lock here.
            check(n, c, inside_transaction=True)
            n.execute(SCHEMA)
            for action in ('UPDATE', 'DELETE'):
                n.execute('CREATE TRIGGER IF NOT EXISTS shortage_1031_no_' + action.lower() +
                    ' BEFORE ' + action + ' ON ' + TABLE + " BEGIN SELECT RAISE(ABORT,'shortage_audit_append_only'); END")
            n.execute('CREATE TRIGGER IF NOT EXISTS shortage_1031_no_replace BEFORE INSERT ON ' + TABLE +
                ' WHEN EXISTS(SELECT 1 FROM ' + TABLE + " WHERE run_id=NEW.run_id) BEGIN SELECT RAISE(ABORT,'shortage_audit_append_only'); END")
            now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            evidence = dict(saved, backup_path=target, backup_sha256=digest.hexdigest())
            n.execute('INSERT INTO ' + TABLE + ' VALUES(?,?,?,?,?,?,?,?)',
                (RUN_ID, expected_sha, ANCHOR['identity_sha256'], actor, reason, now, digest.hexdigest(),
                 json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
            changed = n.execute("UPDATE x_post_schedule_run SET status='claimed',error_code='',error_message='',"
                "finished_at='',updated_at=?,lease_heartbeat_at=? WHERE id=? AND status='failed_preflight' "
                "AND finished_at=? AND plan_attempted_at=''", (now, now, RUN_ID, ANCHOR['failed_at'])).rowcount
            require(changed == 1, 'original_slot_cas_failed')
            require(dependencies(n, c) == saved['snapshot']['dependencies'], 'central_dependency_drift_before_commit')
            n.commit()
        except BaseException:
            n.rollback(); raise
    return dict(result, applied=True, backup_path=target, backup_sha256=digest.hexdigest())


def wait_plan(make_plan, seconds, interval, record, *, monotonic=time.monotonic, sleep=time.sleep):
    require(0 < seconds <= 86400 and 1 <= interval <= 60, 'bounded_wait_required')
    deadline = monotonic() + seconds
    while True:
        saved = make_plan()  # Exact failure/audits are rechecked on every poll.
        state = saved['snapshot']['state']
        record(dict(state=state, pending_queue_ids=saved['snapshot']['pending_queue_ids'],
                    plan_sha256=saved['plan_sha256']))
        if state == 'ready': return saved
        remaining = deadline - monotonic()
        if remaining <= 0: return saved
        sleep(min(interval, remaining))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('plan', 'wait-plan', 'apply'))
    for name in ('native', 'central', 'commit', 'service-path', 'repository', 'plan', 'expect-plan-sha256',
                 'backup-dir', 'actor', 'reason'): p.add_argument('--' + name)
    p.add_argument('--output', required=True)
    p.add_argument('--wait-seconds', type=int, default=3600)
    p.add_argument('--poll-seconds', type=int, default=30)
    a = p.parse_args()
    # Flush each read-only observation separately so interruption leaves proof.
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    with os.fdopen(os.open(a.output, flags, 0o600), 'w', encoding='utf-8') as stream, \
            os.fdopen(os.open(a.output + '.events.jsonl', flags, 0o600), 'w', encoding='utf-8') as events:
        history = []
        def record(value):
            value = dict(value, observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
            history.append(value)
            events.write(json.dumps(value, ensure_ascii=False) + '\n'); events.flush(); os.fsync(events.fileno())
        try:
            make = lambda: plan(a.native, a.central, a.commit, a.service_path, a.repository)
            if a.action == 'apply':
                document = json.loads(Path(a.plan).read_text(encoding='utf-8'))
                saved = document.get('result', document)
                result = apply(saved, a.expect_plan_sha256, a.backup_dir, a.actor, a.reason)
            elif a.action == 'wait-plan':
                result = wait_plan(make, a.wait_seconds, a.poll_seconds, record)
            else: result = make()
            record(dict(state=result.get('snapshot', {}).get('state'), plan_sha256=result.get('plan_sha256'),
                        applied=result.get('applied', False), finished=True))
            document = dict(result=result, observations=history, platform_called=False)
        except BaseException as exc:
            # Persist only our controlled reason, never exception payloads.
            document = dict(rejected=True, reason=str(exc) if isinstance(exc, RecoveryRejected) else type(exc).__name__,
                            observations=history, platform_called=False)
            record(dict(rejected=True, reason=document['reason']))
            json.dump(document, stream, ensure_ascii=False, indent=2); stream.flush(); os.fsync(stream.fileno())
            raise
        json.dump(document, stream, ensure_ascii=False, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    print(json.dumps(dict(run_id=RUN_ID, state=result.get('snapshot', {}).get('state'),
        plan_sha256=result.get('plan_sha256'), applied=result.get('applied', False), platform_called=False)))


if __name__ == '__main__': main()
