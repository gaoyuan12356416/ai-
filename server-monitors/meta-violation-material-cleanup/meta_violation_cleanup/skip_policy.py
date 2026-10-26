"""Persistent skip decisions derived from this task's immutable DELETE attempts.

No schema or historical outcome is changed.  Worker threads only touch the
in-memory cache; SQLite remains on the execution owner thread.
"""
from copy import deepcopy
import json
import threading

from .throttle import RequestNotSent


ACTOR = 'automation:meta-violation-material-cleanup'
JOB_SOURCE = 'scheduled_violation_cleanup'


def failure_reason(kind, result):
    # Never mine nested prior_result/error fields: they may belong to an
    # accepted or unknown write, or a read-only preflight.
    if (not isinstance(result, dict) or result.get('status') != 'failed' or
            result.get('request_sent') is not True or result.get('api_accepted') is True):
        return None
    response = result.get('response')
    if not isinstance(response, dict):
        return None
    pair = (str(response.get('code')), str(response.get('error_subcode')))
    if pair == ('200', '2490592'):
        return 'account_not_writable'
    if kind == 'ad' and pair == ('100', '2446289'):
        return 'content_unavailable'
    return None


class SkipPolicy:
    def __init__(self):
        self.accounts = {}
        self.bindings = {}
        self.lock = threading.Lock()

    @classmethod
    def from_store(cls, store):
        policy = cls()
        with store._transaction(False) as conn:
            # Use the jobs and attempts(job_id, object_key) indices.  Do not
            # load unrelated tasks or deserialize complete historical jobs.
            jobs = [row['job_id'] for row in conn.execute(
                'SELECT job_id,metadata FROM fb_asset_delete_v2_jobs WHERE actor=?', (ACTOR,))
                if json.loads(row['metadata']).get('job_source') == JOB_SOURCE]
            seeds = []
            for job_id in jobs:
                rows = conn.execute('''SELECT a.attempt_id,a.run_id,a.object_key,a.result,
                        a.finished_at,o.kind,o.object_id,o.payload,o.status AS object_status,
                        o.attempt_id AS final_attempt_id,o.result AS final_result,
                        EXISTS (SELECT 1 FROM fb_asset_delete_v2_receipts r WHERE r.object_key=a.object_key) AS has_receipt,
                        EXISTS (SELECT 1 FROM fb_asset_delete_v2_object_locks l WHERE l.object_key=a.object_key) AS has_lock
                    FROM fb_asset_delete_v2_attempts a
                    JOIN fb_asset_delete_v2_objects o ON o.job_id=a.job_id AND o.object_key=a.object_key
                    WHERE a.job_id=? AND a.status='failed' AND a.finished_at IS NOT NULL
                      AND (o.status IN ('failed','blocked') OR a.result LIKE '%account_not_writable%')''', (job_id,))
                for row in rows:
                    result = json.loads(row['result'])
                    reason = failure_reason(row['kind'], result)
                    if not reason:
                        continue
                    persistent_account = (reason == 'account_not_writable' and
                                          result.get('policy_skip_reason') == reason)
                    if not persistent_account:
                        if row['has_receipt'] or row['has_lock']:
                            continue
                        if row['object_status'] == 'failed' and row['attempt_id'] != row['final_attempt_id']:
                            continue
                        if row['object_status'] == 'blocked':
                            source = json.loads(row['final_result']).get('policy_skip_source', {})
                            if source.get('attempt_id') != row['attempt_id']:
                                continue
                        elif row['object_status'] != 'failed':
                            continue
                    payload = json.loads(row['payload'])
                    accounts = payload.get('account_ids', [])
                    creatives = payload.get('creative_ids', [])
                    if len(accounts) != 1 or (row['kind'] == 'ad' and len(creatives) != 1):
                        continue
                    account = str(accounts[0])
                    if result.get('account_id') is not None and str(result['account_id']) != account:
                        continue
                    candidate = {'account_id': account, 'ad_id': row['object_id'],
                                 'creative_id': str(creatives[0]) if creatives else row['object_id']}
                    seeds.append((row['kind'], candidate, result, {
                        'job_id': job_id, 'run_id': row['run_id'],
                        'attempt_id': row['attempt_id'], 'object_key': row['object_key']}, row['finished_at']))
            legacy_accounts = {c['account_id'] for kind,c,result,_,_ in seeds
                               if failure_reason(kind,result) == 'account_not_writable'
                               and not result.get('policy_skip_reason')}
            successes = {}
            if legacy_accounts:
                for job_id in jobs:
                    rows = conn.execute('''SELECT a.result,a.finished_at,o.payload
                        FROM fb_asset_delete_v2_attempts a
                        JOIN fb_asset_delete_v2_objects o ON o.job_id=a.job_id AND o.object_key=a.object_key
                        WHERE a.job_id=? AND a.status='deleted' AND a.finished_at IS NOT NULL''', (job_id,))
                    for row in rows:
                        result = json.loads(row['result'])
                        accounts = json.loads(row['payload']).get('account_ids', [])
                        if (len(accounts) != 1 or str(accounts[0]) not in legacy_accounts or
                                result.get('request_sent') is not True or result.get('api_accepted') is not True):
                            continue
                        account = str(accounts[0])
                        successes[account] = max(successes.get(account, ''), row['finished_at'])
            for kind,candidate,result,source,finished_at in seeds:
                if (failure_reason(kind,result) == 'account_not_writable' and
                        not result.get('policy_skip_reason') and
                        successes.get(candidate['account_id'], '') > finished_at):
                    continue
                policy.observe(kind,candidate,result,source)
            # A legacy failure becomes an explicit durable account policy once
            # a new attempt has actually been skipped for it.  Preserve that
            # decision without tagging or modifying the historical failure.
            # Validate the source attempt instead of trusting a nested error.
            verified_sources = {}
            for job_id in jobs:
                rows = conn.execute('''SELECT a.result,o.payload
                    FROM fb_asset_delete_v2_attempts a
                    JOIN fb_asset_delete_v2_objects o ON o.job_id=a.job_id AND o.object_key=a.object_key
                    WHERE a.job_id=? AND a.status='blocked' AND a.finished_at IS NOT NULL''', (job_id,))
                for row in rows:
                    skipped = json.loads(row['result'])
                    if (skipped.get('status') != 'blocked' or skipped.get('request_sent') is not False or
                            skipped.get('policy_skip_reason') != 'account_not_writable'):
                        continue
                    source = skipped.get('policy_skip_source', {})
                    attempt_id = source.get('attempt_id')
                    if not isinstance(attempt_id, str):
                        continue
                    if attempt_id not in verified_sources:
                        origin = conn.execute('''SELECT a.*,o.kind,o.object_id,o.payload,j.actor,j.metadata
                            FROM fb_asset_delete_v2_attempts a
                            JOIN fb_asset_delete_v2_objects o ON o.job_id=a.job_id AND o.object_key=a.object_key
                            JOIN fb_asset_delete_v2_jobs j ON j.job_id=a.job_id
                            WHERE a.attempt_id=?''', (attempt_id,)).fetchone()
                        verified_sources[attempt_id] = None
                        if (origin and origin['actor'] == ACTOR and origin['status'] == 'failed' and
                                json.loads(origin['metadata']).get('job_source') == JOB_SOURCE):
                            original = json.loads(origin['result'])
                            accounts = json.loads(origin['payload']).get('account_ids', [])
                            if (failure_reason(origin['kind'], original) == 'account_not_writable' and
                                    len(accounts) == 1 and (original.get('account_id') is None or
                                    str(original['account_id']) == str(accounts[0]))):
                                verified_sources[attempt_id] = (origin, original, str(accounts[0]))
                    verified = verified_sources[attempt_id]
                    if not verified:
                        continue
                    origin,original,account = verified
                    accounts = json.loads(row['payload']).get('account_ids', [])
                    if (accounts != [account] or any(source.get(k) != origin[k]
                            for k in ('job_id','run_id','object_key'))):
                        continue
                    policy.observe(origin['kind'], {'account_id':account},
                        dict(original,policy_skip_reason='account_not_writable'), source)
        return policy

    def observe(self, kind, candidate, result, source):
        reason = failure_reason(kind, result)
        if not reason:
            return result
        # Small immutable evidence, never recursively copy a policy annotation.
        proof = dict(source, original_result={
            'status': 'failed', 'request_sent': True,
            'response': deepcopy(result['response'])})
        with self.lock:
            if reason == 'account_not_writable':
                self.accounts.setdefault(str(candidate['account_id']), proof)
            else:
                self.bindings.setdefault(self.binding(candidate), proof)
        return dict(result, policy_skip_reason=reason, policy_skip_source=proof)

    @staticmethod
    def binding(candidate):
        return tuple(str(candidate[k]) for k in ('ad_id', 'account_id', 'creative_id'))

    def result(self, kind, candidate):
        with self.lock:
            proof = self.accounts.get(str(candidate['account_id']))
            reason = 'account_not_writable'
            if proof is None and kind == 'ad':
                proof = self.bindings.get(self.binding(candidate))
                reason = 'content_unavailable'
            if proof is None:
                return None
            return {'status': 'blocked', 'request_sent': False, 'api_accepted': False,
                    'reason': reason, 'policy_skip_reason': reason,
                    'policy_skip_source': deepcopy(proof)}

    def check(self, kind, candidate):
        if self.result(kind, candidate) is not None:
            raise RequestNotSent('policy_skip')

    def outcome(self, kind, candidate, result, source):
        if result.get('request_sent') is False and result.get('code') == 'policy_skip':
            skipped = self.result(kind, candidate)
            if skipped is not None:
                return dict(result, **skipped)
        return self.observe(kind, candidate, result, source)


class RequestBoundarySession:
    """Give an ungoverned serial Graph client the same DELETE boundary hook."""
    def __init__(self, session):
        self.session = session
        self.write_check = lambda: None
        self.account = ''

    def request(self, method, *args, **kwargs):
        if method == 'DELETE':
            self.write_check()
        return self.session.request(method, *args, **kwargs)

    def close(self):
        self.session.close()
