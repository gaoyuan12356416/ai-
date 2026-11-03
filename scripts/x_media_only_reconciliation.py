"""Internal engine for fixed, independently audited media-only unknown incidents.

This is not an operator-supplied policy. Only reviewed maintenance entry points
construct incident contracts; runtime guards and original receipts are unchanged.
"""
import contextlib
import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.pipeline_dispatch.x_prepost_recovery import (
    TABLE as AUTH, SCHEMA as AUTH_SCHEMA, digest, encoded, queue_hash, log_hash, no_post_operation,
)
from scripts.recover_x_prepost_failures import backup, media_facts, stable_job, writable
from scripts.recover_pipeline_x_preparation import RecoveryRejected, require, read_connection, rows, date, document_hash, utcnow


def one(c, sql, params=()):
    result = rows(c, sql, params)
    require(len(result) == 1, 'exact_row_missing')
    return result[0]

def exists(c, table):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()

def comparable(proof):
    p = copy.deepcopy(proof)
    p['jobs'] = [stable_job(j) for j in p['jobs']]
    return p


class AuditedMediaOnlyReconciler:
    def __init__(self, incident, *, clock=utcnow):
        self.c = SimpleNamespace(**copy.deepcopy(incident))
        self.clock = clock
        self.c.HISTORY_AUDIT_FILE = getattr(self.c, 'HISTORY_AUDIT_FILE', 'audit.json')
        self.c.HISTORY_RELEASE_KEY = getattr(self.c, 'HISTORY_RELEASE_KEY', 'previous_sha')
        self.c.HISTORY_CHILDREN_KEY = getattr(self.c, 'HISTORY_CHILDREN_KEY', 'children_before')
        require(all(re.fullmatch(r'[a-z0-9][a-z0-9_]*', value) for value in (self.c.AUDIT, self.c.JOURNAL, self.c.AUDIT_ERROR)), 'invalid_audit_table')
        self.c.AUDIT_SCHEMA = 'CREATE TABLE IF NOT EXISTS '+self.c.AUDIT+' (job_id INTEGER PRIMARY KEY,plan_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)'
        self.c.JOURNAL_SCHEMA = 'CREATE TABLE IF NOT EXISTS '+self.c.JOURNAL+' (job_id INTEGER PRIMARY KEY,plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,backups_json TEXT NOT NULL,updated_at TEXT NOT NULL)'


    def collect(self, c, n):
        q = one(n, 'SELECT * FROM x_post_queue WHERE id=?', (self.c.QID,))
        logs = rows(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (self.c.QID,))
        require(len(logs) == 1, 'log_not_unique')
        jobs = rows(c, 'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY stage', (self.c.KEY,))
        require(len(jobs) == 2 and {j['id'] for j in jobs} == {self.c.PREP_ID, self.c.JOB_ID}, 'job_scope_changed')
        for job in jobs:
            require(json.loads(job['source_meta_json']).get('canonical_fingerprint') == canonical_fingerprint(q, 'id'), 'canonical_identity_changed')
        proof = {'queue': q, 'log': logs[0], 'jobs': jobs, 'parent': one(n, 'SELECT * FROM x_post_manual_run WHERE id=?', (self.c.PARENT_ID,)), 'native_ops': rows(n, 'SELECT * FROM pipeline_x_operation WHERE source_key=? ORDER BY operation_id', (self.c.KEY,)), 'claims': rows(n, 'SELECT * FROM pipeline_x_claim WHERE source_key=? ORDER BY stage,generation', (self.c.KEY,)), 'native_checkpoints': rows(n, 'SELECT * FROM pipeline_x_checkpoint WHERE source_key=? ORDER BY generation,phase', (self.c.KEY,)), 'media': media_facts(n, self.c.QID), 'holds': rows(n, 'SELECT * FROM pipeline_canonical_hold WHERE source_key=?', (self.c.KEY,)), 'reposts': rows(n, 'SELECT * FROM x_post_repost_ledger WHERE queue_id=?', (self.c.QID,)), 'publications': rows(c, 'SELECT * FROM pipeline_publication WHERE source_key=?', (self.c.KEY,)), 'siblings': rows(n, 'SELECT id FROM x_post_queue WHERE manual_run_id=? ORDER BY id', (self.c.PARENT_ID,))}
        for name, table, order in (('central_ops', 'pipeline_operation', 'operation_id'), ('history', 'pipeline_claim_history', 'job_id,generation'), ('central_checkpoints', 'pipeline_checkpoint', 'checkpoint_id'), ('receipts', 'pipeline_receipt', 'receipt_id'), ('deliveries', 'pipeline_delivery', 'delivery_id'), ('events', 'pipeline_event', 'id'), ('resources', 'pipeline_resource', 'resource_key'), ('legacy_resources', 'pipeline_legacy_resource', 'resource_key')):
            proof[name] = rows(c, 'SELECT * FROM ' + table + ' WHERE job_id IN (?,?) ORDER BY ' + order, (self.c.PREP_ID, self.c.JOB_ID))
        return proof

    def initial_proof(self, c, n, now):
        p = self.collect(c, n)
        q, log = (p['queue'], p['log'])
        require(queue_hash(q) == self.c.EXPECTED['queue'] and digest(log) == self.c.EXPECTED['log'], 'exact_original_identity_changed')
        require((q['status'], q['account_id'], q['manual_run_id'], q['delivery_mode']) == ('failed', self.c.ACCOUNT_ID, self.c.PARENT_ID, 'direct') and (log['id'], log['status'], log['attempt_count'], log['unknown_outcome']) == (self.c.LOG_ID, 'failed', 1, 1) and (log['error_code'] == log['error_message'] == self.c.ERROR) and (not any((log[k] for k in ('x_media_id', 'x_post_id', 'published_at')))) and all((log[k] for k in ('post_text', 'long_url', 'short_url'))), 'not_exact_original_unknown')
        jobs = {j['stage']: j for j in p['jobs']}
        pub, prep = (jobs['publish'], jobs['prepare'])
        require(prep['id'] == self.c.PREP_ID and prep['status'] == 'completed' and (pub['id'] == self.c.JOB_ID) and (pub['generation'] == 1) and (pub['status'] == 'unknown') and (pub['node_id'] == self.c.PUBLISH_NODE) and (pub['worker_id'] == self.c.WORKER) and (pub['claim_hash'] == self.c.CLAIM_SHA) and (pub['external_intent'] == 1) and (pub['error_code'] == self.c.ERROR) and (not pub['hold_reason']) and (not pub['canonical_receipt']), 'original_execution_changed')
        require(all((not j['lease_expires_at'] or date(j['lease_expires_at']) < now for j in p['jobs'])) and date(log['updated_at']) < now - dt.timedelta(seconds=120), 'execution_not_settled')
        for name in ('native_ops', 'central_ops', 'claims', 'history'):
            require(digest(p[name]) == self.c.EXPECTED[name], 'historical_' + name + '_changed')
        for name in ('parent', 'native_checkpoints', 'central_checkpoints'):
            if name in self.c.EXPECTED:
                require(digest(p[name]) == self.c.EXPECTED[name], 'historical_' + name + '_changed')
        require(len(p['native_ops']) == self.c.OP_COUNT and all((r['generation'] == 1 and no_post_operation(r['operation']) for r in p['native_ops'])), 'not_exact_media_only_operations')
        fields = ('operation_id', 'generation', 'operation', 'request_sha256')
        require({tuple((r[k] for k in fields)) for r in p['native_ops']} == {tuple((r[k] for k in fields)) for r in p['central_ops']}, 'operation_ledgers_disagree')
        require(not p['holds'] and (not p['reposts']) and (not p['publications']) and (not p['legacy_resources']), 'other_hold_or_publication')
        require(p['resources'] == [{'resource_key': 'x:account:' + str(self.c.ACCOUNT_ID), 'job_id': self.c.JOB_ID, 'generation': 1, 'quarantined': 1, 'legacy': 0}], 'quarantine_resource_changed')
        require(all((r['phase'] in ('x_preflight', 'x_media_initialized') for r in p['native_checkpoints'] + p['central_checkpoints'])), 'later_checkpoint')
        delivery = [r for r in p['deliveries'] if r['delivery_id'] == self.c.RECEIPT_ID]
        require(len(delivery) == 1 and delivery[0]['job_id'] == self.c.JOB_ID and (delivery[0]['generation'] == 1) and (delivery[0]['claim_hash'] == self.c.CLAIM_SHA) and (delivery[0]['request_sha256'] == self.c.RECEIPT_SHA) and (json.loads(delivery[0]['response_json']) == {'accepted': True, 'status': 'unknown'}), 'original_terminal_delivery_missing')
        failures = [r for r in p['events'] if r['event_type'] == 'failed']
        require(len(failures) == 1 and failures[0]['job_id'] == self.c.JOB_ID and (json.loads(failures[0]['details_json']) == {'code': self.c.ERROR, 'unknown': True}), 'historical_failure_changed')
        parent = p['parent']
        require(parent['trigger_source'] == 'auto_template' and parent['status'] == 'needs_review' and ((parent['expected_count'], parent['queued_count'], parent['unknown_count'], parent['published_count'], parent['failed_count']) == (1, 1, 1, 0, 0)) and (not parent['error_code']) and (not parent['error_message']) and (p['siblings'] == [{'id': self.c.QID}]), 'parent_scope_changed')
        descriptor = json.loads(next((r['descriptor_json'] for r in p['claims'] if r['stage'] == 'publish')))
        artifact = descriptor['artifact']
        require(descriptor['kind'] == 'x_publish' and descriptor['request']['body'] == log['post_text'] and (descriptor['account_resource'] == 'x:account:' + str(self.c.ACCOUNT_ID)), 'frozen_descriptor_changed')
        if q['random_template_enabled']:
            from features.x_posts.random_template import validate_receipt
            render = p['media']['x_post_random_template_preparation']
            require(len(render) == 1 and render[0]['status'] == 'ready', 'render_not_ready')
            request, result = (json.loads(render[0]['request_json']), json.loads(render[0]['result_json']))
            validate_receipt(request, result)
            expected = {'url': result['output_url'], 'sha256': result['output_sha256'], 'size': result['output_size'], 'mime': 'video/mp4', 'duration_seconds': result['probe']['duration'], 'profile': result['profile'], 'recipe': result['recipe']}
            require(expected == artifact and request['queue_id'] == self.c.QID, 'render_identity_changed')
        require(artifact.get('sha256') and artifact.get('size'), 'artifact_missing')
        if getattr(self.c, 'FROZEN_ARTIFACT_SHA256', None):
            require(artifact['sha256'] == self.c.FROZEN_ARTIFACT_SHA256 and artifact['size'] == self.c.FROZEN_ARTIFACT_SIZE,
                    'reviewed_artifact_changed')
        require(not (exists(n, AUTH) and n.execute('SELECT 1 FROM ' + AUTH + ' WHERE source_key=?', (self.c.KEY,)).fetchone()), 'already_authorized')
        return p

    def plan(self, central, native, now=None):
        now = now or self.clock()
        with contextlib.closing(read_connection(central)) as c, contextlib.closing(read_connection(native)) as n:
            c.execute('BEGIN')
            n.execute('BEGIN')
            require([r[0] for r in c.execute('SELECT node_id FROM pipeline_node ORDER BY node_id')] == ['cpu-main', 'hk-gpu'], 'node_inventory_changed')
            proof = self.initial_proof(c, n, now)
        result = {'kind': self.c.PLAN_KIND, 'version': 1, 'central': str(Path(central).resolve()), 'native': str(Path(native).resolve()), 'proof': proof, 'captured_at': now.isoformat()}
        if self.c.INCIDENT_SHA:
            result['incident_sha256'] = self.c.INCIDENT_SHA
        result['plan_sha256'] = document_hash(result)
        return result

    def validate_plan(self, saved):
        require(saved.get('kind') == self.c.PLAN_KIND and saved.get('version') == 1 and (saved.get('plan_sha256') == document_hash(saved)) and (saved['proof']['queue']['id'] == self.c.QID) and (saved['proof']['log']['id'] == self.c.LOG_ID), 'plan_changed')

        if self.c.INCIDENT_SHA:
            require(saved.get('incident_sha256') == self.c.INCIDENT_SHA, 'incident_contract_changed')

    def validate_saved_origin(self, saved):
        """Check a committed audit against the independently reviewed manifest.

        Used only for later read-only Auto observation. Current mutable native
        state is checked separately; old unknown facts stay in this snapshot.
        """
        self.validate_plan(saved)
        proof = saved['proof']
        require(queue_hash(proof['queue']) == self.c.EXPECTED['queue'], 'original_queue_changed')
        for name, expected in self.c.EXPECTED.items():
            if name != 'queue':
                require(digest(proof[name]) == expected, 'original_'+name+'_changed')
        require(proof['log']['unknown_outcome'] == 1 and proof['log']['attempt_count'] == 1
                and len(proof['native_ops']) == self.c.OP_COUNT
                and all(r['generation'] == 1 and no_post_operation(r['operation']) for r in proof['native_ops']),
                'original_execution_not_reviewed')

    def historical_worker(self, history_dir=None, release_root=None):
        history_dir = history_dir or self.c.HISTORY_DIR
        release_root = release_root or self.c.RELEASE_ROOT
        checked = {}
        for base, files in ((history_dir, self.c.HISTORY_FILES), (release_root, self.c.RELEASE_FILES)):
            for rel, expected in files.items():
                actual = hashlib.sha256((Path(base) / rel).read_bytes()).hexdigest()
                require(actual == expected, 'historical_worker_file_changed')
                checked[rel] = actual
        audit = json.loads((Path(history_dir) / self.c.HISTORY_AUDIT_FILE).read_text())
        children = audit[self.c.HISTORY_CHILDREN_KEY]
        require(audit[self.c.HISTORY_RELEASE_KEY] == self.c.RELEASE
                and str(self.c.WORKER_PID) in {str(pid) for pid in children}, 'worker_pid_release_not_bound')
        return {'release': self.c.RELEASE, 'worker_id': self.c.WORKER, 'pid': self.c.WORKER_PID, 'files': checked}

    def node_evidence(self, saved, node_id, outbox, now=None, *, history_dir=None, release_root=None):
        self.validate_plan(saved)
        require(node_id in ('cpu-main', 'hk-gpu'), 'node_scope')
        result = {'node_id': node_id, 'plan_sha256': saved['plan_sha256'], 'captured_at': (now or self.clock()).isoformat(), 'receipts': []}
        with contextlib.closing(read_connection(outbox)) as c:
            for row in c.execute('SELECT id,route,payload,acked_at FROM receipt_outbox ORDER BY id'):
                payload = json.loads(row['payload'])
                if payload.get('job_id') not in (self.c.PREP_ID, self.c.JOB_ID):
                    continue
                safe = dict(payload)
                if 'claim_token' in safe:
                    safe['claim_token_sha256'] = hashlib.sha256(str(safe.pop('claim_token')).encode()).hexdigest()
                result['receipts'].append({'id': row['id'], 'route': row['route'], 'acked_at': row['acked_at'], 'payload_sha256': digest(payload), 'payload': safe})
        if node_id == self.c.PUBLISH_NODE:
            result['historical_worker'] = self.historical_worker(history_dir, release_root)
        return result

    def validate_nodes(self, saved, evidence, now):
        require(sorted((e['node_id'] for e in evidence)) == ['cpu-main', 'hk-gpu'], 'both_nodes_required')
        failures = []
        for e in evidence:
            require(e['plan_sha256'] == saved['plan_sha256'] and 0 <= (now - date(e['captured_at'])).total_seconds() <= 120, 'node_evidence_stale')
            expected = self.c.RECEIPTS_BY_NODE[e['node_id']]
            require(len(e['receipts']) == len(expected) and {r['id'] for r in e['receipts']} == set(expected), 'historical_receipt_set_changed')
            for row in e['receipts']:
                p = row['payload']
                require([row['route'], row['payload_sha256'], digest(p)] == list(expected[row['id']]), 'historical_receipt_changed')
                require(p['job_id'] in (self.c.PREP_ID, self.c.JOB_ID) and p['generation'] == 1 and (row['acked_at'] is not None), 'pending_or_other_generation_receipt')
                require(p.get('phase') not in ('x_media_uploaded', 'x_source_published', 'x_target_reposted') and (not (p.get('result') or {}).get('post_id')), 'later_or_success_receipt')
                if row['route'] == 'jobs/fail' or p.get('unknown_outcome'):
                    require(e['node_id'] == self.c.PUBLISH_NODE and row['id'] == self.c.RECEIPT_ID and (row['route'] == 'jobs/fail') and (row['payload_sha256'] == self.c.RECEIPT_SHA) and (p['job_id'] == self.c.JOB_ID) and (p['unknown_outcome'] is True) and (p['error_code'] == self.c.ERROR) and (p['worker_id'] == self.c.WORKER) and (p['claim_token_sha256'] == self.c.CLAIM_SHA), 'not_original_unknown_receipt')
                    failures.append(row)
        require(len(failures) == 1, 'original_ack_missing')
        worker = next((e for e in evidence if e['node_id'] == self.c.PUBLISH_NODE)).get('historical_worker')
        require(worker == {'release': self.c.RELEASE, 'worker_id': self.c.WORKER, 'pid': self.c.WORKER_PID, 'files': dict(self.c.HISTORY_FILES, **self.c.RELEASE_FILES)}, 'historical_worker_proof_changed')

    def assert_state(self, saved, c, n, native_armed=False):
        old = saved['proof']
        fresh = self.collect(c, n)
        pub = next((j for j in fresh['jobs'] if j['id'] == self.c.JOB_ID))
        require(pub['hold_reason'] == self.c.HOLD, 'central_reconciliation_hold_missing')
        pub['hold_reason'] = next((j for j in old['jobs'] if j['id'] == self.c.JOB_ID))['hold_reason']
        if native_armed:
            require(fresh['queue']['status'] == 'queued' and fresh['log']['status'] == 'reserved' and (fresh['log']['unknown_outcome'] == 0) and (fresh['log']['attempt_count'] == 1), 'armed_native_state_changed')
            for key in ('status', 'updated_at'):
                fresh['queue'][key] = old['queue'][key]
            for key in ('status', 'unknown_outcome', 'updated_at'):
                fresh['log'][key] = old['log'][key]
            parent = fresh['parent']
            require(parent['status'] == 'running' and parent['unknown_count'] == 0 and (parent['published_count'] == 0) and (parent['failed_count'] == 0) and (parent['queued_count'] == 1), 'armed_parent_changed')
            for key in ('status', 'unknown_count', 'updated_at', 'finished_at'):
                parent[key] = old['parent'][key]
        require(comparable(fresh) == comparable(old), 'proof_state_drift')

    def current_authorization(self, saved, n, *, allow_missing=False):
        found = rows(n, 'SELECT * FROM ' + AUTH + ' WHERE source_key=?', (self.c.KEY,))
        if not found and allow_missing:
            return None
        require(len(found) == 1, 'authorization_changed')
        auth = found[0]
        p = saved['proof']
        descriptor = json.loads(next((r['descriptor_json'] for r in p['claims'] if r['stage'] == 'publish')))
        expected = {'source_key': self.c.KEY, 'stage': 'publish', 'from_generation': 1, 'to_generation': 2, 'prior_attempts': 1, 'queue_sha256': queue_hash(p['queue']), 'log_sha256': log_hash(p['log']), 'artifact_sha256': digest(descriptor['artifact']), 'plan_sha256': saved['plan_sha256'], 'state': 'armed', 'started_at': '', 'proof_json': encoded({'reconciliation': self.c.AUDIT, 'original_proof': p})}
        require(all((auth[k] == value for k, value in expected.items())), 'authorization_changed')
        audit = one(n, 'SELECT * FROM ' + self.c.AUDIT + ' WHERE job_id=?', (self.c.JOB_ID,))
        require(audit['plan_sha256'] == saved['plan_sha256'] and auth['actor'] == json.loads(audit['evidence_json'])['actor'] and (auth['created_at'] == audit['created_at']), 'authorization_audit_changed')
        return auth

    def append_audit(self, c, saved, evidence, backups, actor, now):
        c.execute(self.c.AUDIT_SCHEMA)
        for action in ('UPDATE', 'DELETE'):
            c.execute('CREATE TRIGGER IF NOT EXISTS ' + self.c.AUDIT + '_no_' + action.lower() + ' BEFORE ' + action + ' ON ' + self.c.AUDIT + " BEGIN SELECT RAISE(ABORT,'" + self.c.AUDIT_ERROR + "'); END")
        c.execute('CREATE TRIGGER IF NOT EXISTS ' + self.c.AUDIT + '_no_replace BEFORE INSERT ON ' + self.c.AUDIT + ' WHEN EXISTS(SELECT 1 FROM ' + self.c.AUDIT + " WHERE job_id=NEW.job_id) BEGIN SELECT RAISE(ABORT,'" + self.c.AUDIT_ERROR + "'); END")
        original = c.execute('SELECT plan_sha256 FROM ' + self.c.AUDIT + ' WHERE job_id=?', (self.c.JOB_ID,)).fetchone()
        if original:
            require(original[0] == saved['plan_sha256'], 'different_reconciliation_audit')
            return
        body = {'plan': saved, 'node_evidence': evidence, 'backups': backups, 'actor': actor, 'finding': 'exact_generation_stopped_in_media_upload_before_Post', 'original_unknown_preserved': True}
        c.execute('INSERT INTO ' + self.c.AUDIT + ' VALUES(?,?,?,?)', (self.c.JOB_ID, saved['plan_sha256'], encoded(body), now))

    def apply(self, saved, evidence, backup_dir, actor, now=None, stop_after=None):
        clock = (lambda: now) if now is not None else self.clock
        self.validate_plan(saved)
        started = clock()
        timestamp = started.isoformat()
        require(isinstance(actor, str) and actor.strip(), 'actor_required')
        with contextlib.closing(read_connection(saved['central'])) as c:
            journal = c.execute('SELECT * FROM ' + self.c.JOURNAL + ' WHERE job_id=?', (self.c.JOB_ID,)).fetchone() if exists(c, self.c.JOURNAL) else None
            if journal:
                require(journal['plan_sha256'] == saved['plan_sha256'], 'different_reconciliation_plan')
                if journal['state'] == 'committed':
                    return {'already_applied': True, 'queue_id': self.c.QID, 'platform_called': False}
        self.validate_nodes(saved, evidence, clock())
        backups = json.loads(journal['backups_json']) if journal else []
        if not journal:
            for label in ('central', 'native'):
                path = backup(saved[label], backup_dir, str(self.c.QID) + '-' + label)
                backups.append({'database': label, 'path': path, 'sha256': file_hash(path)})
        with contextlib.closing(writable(saved['central'])) as c, contextlib.closing(writable(saved['native'])) as n:
            c.execute('BEGIN IMMEDIATE')
            n.execute('BEGIN')
            try:
                self.validate_nodes(saved, evidence, clock())
                c.execute(self.c.JOURNAL_SCHEMA)
                journal = c.execute('SELECT * FROM ' + self.c.JOURNAL + ' WHERE job_id=?', (self.c.JOB_ID,)).fetchone()
                if not journal:
                    require(comparable(self.initial_proof(c, n, clock())) == comparable(saved['proof']), 'plan_state_drift')
                    self.append_audit(c, saved, evidence, backups, actor, timestamp)
                    c.execute('INSERT INTO ' + self.c.JOURNAL + ' VALUES(?,?,?,?,?)', (self.c.JOB_ID, saved['plan_sha256'], 'staged', encoded(backups), timestamp))
                    changed = c.execute("UPDATE pipeline_job SET hold_reason=? WHERE id=? AND generation=1 AND status='unknown' AND hold_reason=''", (self.c.HOLD, self.c.JOB_ID)).rowcount
                    require(changed == 1, 'central_stage_cas_failed')
                else:
                    require(journal['plan_sha256'] == saved['plan_sha256'] and journal['state'] == 'staged', 'journal_state_changed')
                c.commit()
                n.rollback()
            except BaseException:
                c.rollback()
                n.rollback()
                raise
            if stop_after == 'central_staged':
                raise RecoveryRejected('injected_after_central_stage')
            c.execute('BEGIN IMMEDIATE')
            n.execute('BEGIN IMMEDIATE')
            try:
                self.validate_nodes(saved, evidence, clock())
                n.execute(AUTH_SCHEMA)
                auth = self.current_authorization(saved, n, allow_missing=True)
                self.assert_state(saved, c, n, native_armed=bool(auth))
                if not auth:
                    self.append_audit(n, saved, evidence, backups, actor, timestamp)
                    p = saved['proof']
                    descriptor = json.loads(next((r['descriptor_json'] for r in p['claims'] if r['stage'] == 'publish')))
                    n.execute('INSERT INTO ' + AUTH + ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (self.c.KEY, 'publish', 1, 2, 1, queue_hash(p['queue']), log_hash(p['log']), digest(descriptor['artifact']), saved['plan_sha256'], 'armed', actor, timestamp, '', encoded({'reconciliation': self.c.AUDIT, 'original_proof': p})))
                    changed = n.execute("UPDATE x_post_publish_log SET status='reserved',unknown_outcome=0,updated_at=? WHERE id=? AND status='failed' AND attempt_count=1 AND unknown_outcome=1", (timestamp, self.c.LOG_ID)).rowcount
                    require(changed == 1, 'native_log_cas_failed')
                    require(n.execute("UPDATE x_post_queue SET status='queued',updated_at=? WHERE id=? AND status='failed'", (timestamp, self.c.QID)).rowcount == 1, 'native_queue_cas_failed')
                    from features.x_posts.service import XPostStore
                    XPostStore.__new__(XPostStore)._sync_run(n, self.c.QID, timestamp)
                    n.execute("UPDATE x_post_manual_run SET status='running',updated_at=? WHERE id=? AND trigger_source='auto_template' AND status='queued'", (timestamp, self.c.PARENT_ID))
                n.commit()
                c.rollback()
            except BaseException:
                n.rollback()
                c.rollback()
                raise
            if stop_after == 'native_armed':
                raise RecoveryRejected('injected_after_native_arm')
            c.execute('BEGIN IMMEDIATE')
            n.execute('BEGIN IMMEDIATE')
            try:
                self.validate_nodes(saved, evidence, clock())
                self.assert_state(saved, c, n, native_armed=True)
                self.current_authorization(saved, n)
                current = one(c, 'SELECT * FROM pipeline_job WHERE id=?', (self.c.JOB_ID,))
                meta = json.loads(current['source_meta_json'])
                meta.update(reconcile_only=False, legacy_inflight=False, unknown_outcome=False)
                changed = c.execute("UPDATE pipeline_job SET status='ready',hold_reason='',external_intent=0,started_at='',finished_at='',lease_expires_at='',claim_hash='',node_id='',worker_id='',source_meta_json=?,updated_at=? WHERE id=? AND generation=1 AND status='unknown' AND hold_reason=?", (encoded(meta), timestamp, self.c.JOB_ID, self.c.HOLD)).rowcount
                require(changed == 1, 'central_admission_cas_failed')
                require(c.execute('DELETE FROM pipeline_resource WHERE resource_key=? AND job_id=? AND generation=1 AND quarantined=1 AND legacy=0', ('x:account:' + str(self.c.ACCOUNT_ID), self.c.JOB_ID)).rowcount == 1, 'quarantine_cas_failed')
                c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)', (self.c.JOB_ID, self.c.EVENT, encoded({'plan_sha256': saved['plan_sha256'], 'original_receipt_sha256': self.c.RECEIPT_SHA, 'actor': actor, 'original_attempts': 1, 'from_generation': 1, 'to_generation': 2}), timestamp))
                c.execute('UPDATE ' + self.c.JOURNAL + " SET state='committed',updated_at=? WHERE job_id=?", (timestamp, self.c.JOB_ID))
                c.commit()
                n.rollback()
            except BaseException:
                c.rollback()
                n.rollback()
                raise
        return {'already_applied': False, 'queue_id': self.c.QID, 'log_id': self.c.LOG_ID, 'plan_sha256': saved['plan_sha256'], 'from_generation': 1, 'to_generation': 2, 'preserved_attempt_count': 1, 'platform_called': False, 'backups': backups}
