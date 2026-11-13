"""Explicit date-bounded TT recovery; default preview, preserve native identities.

Only selection failures with no material, binding, operation or platform evidence
can resume. Neither appointment nor reporting day changes. A missing account
snapshot may be restored only from unanimous frozen siblings of the same day,
account and template version. No network or publishing request is made here.
"""
import argparse
from contextlib import closing
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3

ERRORS = {'tt_auto_metric_window_not_ready', 'tt_upstream_rejected', 'tt_account_source_unavailable'}
SNAPSHOT = ('account_username', 'account_display_name', 'drama_language',
            'account_setting_version', 'account_settings_json')


def dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def sha(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def connection(native, central, write=False):
    c = sqlite3.connect('file:' + str(native) + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('ATTACH DATABASE ? AS dispatch', ('file:' + str(central) + ('?mode=rw' if write else '?mode=ro'),))
    if not write:
        c.execute('PRAGMA query_only=ON')
    return c


def inspect(c, dates, now):
    result, excluded = [], []
    rows = c.execute("SELECT t.*,r.shanghai_date FROM tt_auto_task t JOIN tt_auto_run r ON r.id=t.run_id WHERE r.shanghai_date IN (" + ','.join('?' for _ in dates) + ') ORDER BY t.id', dates).fetchall()
    for raw in rows:
        t, reason = dict(raw), ''
        key = 'tt:auto:' + str(t['id'])
        jobs = [dict(x) for x in c.execute('SELECT * FROM dispatch.pipeline_job WHERE source_key=?', (key,))]
        if t['status'] != 'failed' or t['error_code'] not in ERRORS:
            reason = 'not_allowed_selection_failure'
        elif any(t[k] for k in ('publish_id', 'publish_url', 'unknown_outcome', 'publish_attempt_count', 'preparation_attempt_count', 'claim_token', 'material_id', 'gpu_job_id', 'prepared_media_url', 'published_at_utc')):
            reason = 'native_execution_evidence'
        elif len(jobs) != 1 or (jobs[0]['channel'], jobs[0]['family'], jobs[0]['stage'], jobs[0]['status']) != ('tt', 'auto', 'prepare', 'failed'):
            reason = 'central_not_failed_preparation'
        elif any(jobs[0][k] for k in ('external_intent', 'canonical_receipt')) or jobs[0]['hold_reason'] not in ('', 'adapter_unavailable') or str(jobs[0]['lease_expires_at']) > now:
            reason = 'central_intent_receipt_hold_or_lease'
        elif any(c.execute('SELECT 1 FROM ' + table + ' WHERE source_key=? LIMIT 1', (key,)).fetchone()
                 for table in ('pipeline_tt_claim', 'pipeline_tt_operation', 'pipeline_canonical_hold', 'dispatch.pipeline_publication')):
            reason = 'native_binding_operation_hold_or_publication'
        elif c.execute('SELECT 1 FROM tt_auto_material_ledger WHERE task_id=? LIMIT 1', (t['id'],)).fetchone():
            reason = 'native_material_reservation'
        elif any(c.execute('SELECT 1 FROM dispatch.' + table + ' WHERE job_id=? LIMIT 1', (jobs[0]['id'],)).fetchone()
                 for table in ('pipeline_operation', 'pipeline_receipt', 'pipeline_checkpoint', 'pipeline_delivery', 'pipeline_resource', 'pipeline_legacy_resource')):
            reason = 'central_operation_receipt_or_resource'
        snapshot, donors = None, []
        if not reason and (t['drama_language'] == 'und' or json.loads(t['account_settings_json']).get('configured') is False):
            siblings = c.execute("SELECT s.* FROM tt_auto_task s JOIN tt_auto_run r ON r.id=s.run_id WHERE s.account_id=? AND s.template_id=? AND s.template_version=? AND r.shanghai_date=? AND s.id<>? AND s.drama_language<>'und' AND s.account_setting_version>0", (t['account_id'], t['template_id'], t['template_version'], t['shanghai_date'], t['id'])).fetchall()
            choices = {dump({k: row[k] for k in SNAPSHOT}) for row in siblings}
            if len(choices) != 1:
                reason = 'missing_or_ambiguous_frozen_account_snapshot'
            else:
                snapshot = json.loads(next(iter(choices)))
                donors = [row['id'] for row in siblings]
        if reason:
            excluded.append({'task_id': t['id'], 'reason': reason})
            continue
        j = jobs[0]
        result.append({'task_id': t['id'], 'run_id': t['run_id'], 'job_id': j['id'], 'source_key': key,
            'date': t['shanghai_date'], 'scheduled_at_utc': t['scheduled_at_utc'], 'account_id': t['account_id'],
            'template_id': t['template_id'], 'template_version': t['template_version'],
            'error_code': t['error_code'], 'central_error': j['error_code'], 'generation': j['generation'],
            'account_snapshot': snapshot, 'snapshot_donor_task_ids': donors})
    return result, excluded


def make_plan(native, central, dates, now):
    if not 1 <= len(dates) <= 2 or len(set(dates)) != len(dates):
        raise ValueError('one_or_two_exact_dates_required')
    for date in dates:
        dt.date.fromisoformat(date)
    with closing(connection(native, central)) as c:
        items, excluded = inspect(c, dates, now.isoformat())
    plan = {'kind': 'authorized_tt_unstarted_daily_recovery_v1', 'dates': dates,
            'created_at': now.isoformat(), 'apply_before': (now + dt.timedelta(hours=2)).isoformat(),
            'items': items, 'excluded': excluded}
    plan['sha256'] = sha(plan)
    return plan


def apply_plan(native, central, plan, expected, backup_dir, now):
    unsigned = {k: v for k, v in plan.items() if k != 'sha256'}
    if expected != plan.get('sha256') or sha(unsigned) != expected:
        raise ValueError('plan_hash_mismatch')
    if now >= dt.datetime.fromisoformat(plan['apply_before']):
        raise ValueError('recovery_application_window_expired')
    folder = Path(backup_dir)
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    (folder / 'approved-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    # Online backup for evidence only. Never restore an old publishing database.
    for source, label in ((native, 'native'), (central, 'dispatch')):
        with closing(sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True)) as source_c, closing(sqlite3.connect(str(folder / (label + '.sqlite3')))) as destination:
            source_c.backup(destination)
    stamp = now.isoformat(timespec='seconds')
    with closing(connection(native, central, True)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            # Acquire both writer locks before revalidating either ledger.
            c.execute('UPDATE dispatch.pipeline_config SET version=version WHERE 0')
            already = c.execute("SELECT count(*) FROM tt_auto_event WHERE event_type='authorized_unstarted_daily_recovery' AND message=?", (expected,)).fetchone()[0]
            if already:
                raise ValueError('recovery_already_recorded_reconcile_before_retry')
            items, excluded = inspect(c, plan['dates'], stamp)
            if items != plan['items'] or excluded != plan['excluded'] or not items:
                raise ValueError('recovery_scope_changed')
            for item in items:
                t_id, j_id = item['task_id'], item['job_id']
                if item['account_snapshot']:
                    c.execute('UPDATE tt_auto_task SET ' + ','.join(k + '=?' for k in SNAPSHOT) + ' WHERE id=?', (*[item['account_snapshot'][k] for k in SNAPSHOT], t_id))
                c.execute("UPDATE tt_auto_task SET status='pending',claim_phase='',claim_worker='',claim_token='',lease_expires_at_utc='',next_attempt_at_utc='',error_code='',error_message='',finished_at_utc='',updated_at=? WHERE id=?", (stamp, t_id))
                c.execute("UPDATE tt_auto_run SET status='queued',error_code='',error_message='',finished_at_utc='',updated_at=? WHERE id=?", (stamp, item['run_id']))
                details = dict(item, plan_sha256=expected, actor='operator_authorized', recovery_applied_at=stamp)
                c.execute("INSERT INTO tt_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(?,?,'authorized_unstarted_daily_recovery','failed','pending',?,?,?)", (item['run_id'], t_id, expected, dump(details), stamp))
                row = c.execute('SELECT source_meta_json FROM dispatch.pipeline_job WHERE id=?', (j_id,)).fetchone()
                meta = json.loads(row[0]); meta.update(execution_available=True, legacy_inflight=False, reconcile_only=False, source_attempt_count=0)
                c.execute("UPDATE dispatch.pipeline_job SET status='queued',source_status='pending',hold_reason='',node_id='',worker_id='',claim_hash='',lease_expires_at='',started_at='',finished_at='',error_code='',error_message='',source_meta_json=?,updated_at=? WHERE id=?", (dump(meta), stamp, j_id))
                c.execute("INSERT INTO dispatch.pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,'authorized_tt_unstarted_daily_recovery',?,?)", (j_id, dump(details), stamp))
            c.commit()
        except BaseException:
            c.rollback()
            raise
    result = {'applied': len(items), 'plan_sha256': expected, 'backup_dir': str(folder), 'task_ids': [x['task_id'] for x in items]}
    (folder / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native', default='/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3')
    p.add_argument('--central', default='/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')
    p.add_argument('--dates', nargs='+')
    p.add_argument('--plan', required=True)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--expected-sha256')
    p.add_argument('--backup-dir')
    args = p.parse_args(); now = dt.datetime.now(dt.timezone.utc)
    if args.apply:
        if not args.backup_dir or not args.expected_sha256:
            p.error('apply requires backup-dir and expected-sha256')
        result = apply_plan(args.native, args.central, json.loads(Path(args.plan).read_text()), args.expected_sha256, args.backup_dir, now)
    else:
        if not args.dates:
            p.error('preview requires dates')
        result = make_plan(args.native, args.central, args.dates, now)
        Path(args.plan).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
