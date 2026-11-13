"""Admin operations on an existing X queue and its co-located dispatcher.

Both ledgers are locked in one attached SQLite transaction. No X request,
material reselection, lease expiry recovery, or publication deletion occurs here.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .service import XPostError, XPostStore, utc_now

STOP = 'operator_stopped'
ACTIVE = {'running', 'legacy_running', 'claimed', 'uploading', 'publishing'}


def dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


class LogManager:
    def __init__(self, post_db=None, dispatch_db=None):
        self.post_db = Path(post_db or os.environ.get('X_POST_LOG_DB_PATH', '/var/lib/x-post-automation/accounts.sqlite3'))
        self.dispatch_db = Path(dispatch_db or os.environ.get('PIPELINE_DISPATCH_DB_PATH', '/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3'))

    def connect(self, *, readonly=False):
        mode = 'ro' if readonly else 'rw'
        c = sqlite3.connect(self.post_db.resolve().as_uri() + '?mode=' + mode, uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            c.execute('ATTACH DATABASE ? AS dispatch', (self.dispatch_db.resolve().as_uri() + '?mode=' + mode,))
            c.execute('PRAGMA foreign_keys=ON')
            if readonly:
                c.execute('PRAGMA query_only=ON')
            return c
        except BaseException:
            c.close()
            raise

    @staticmethod
    def install(c):
        c.execute('''CREATE TABLE IF NOT EXISTS x_post_queue_control_audit (
          id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, queue_id INTEGER NOT NULL,
          action TEXT NOT NULL, actor_json TEXT NOT NULL, before_json TEXT NOT NULL,
          result_json TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(operation_id,queue_id))''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_x_post_control_queue ON x_post_queue_control_audit(queue_id,id)')

    @staticmethod
    def snapshot(c, queue_id):
        def one(sql, args):
            row = c.execute(sql, args).fetchone()
            return dict(row) if row else {}
        q = one('SELECT * FROM x_post_queue WHERE id=?', (queue_id,))
        if not q:
            raise XPostError('x_post_queue_not_found', '队列不存在', 404)
        key = 'x:queue:' + str(queue_id)
        log = one('SELECT * FROM x_post_publish_log WHERE queue_id=?', (queue_id,))
        relay = one('SELECT * FROM x_post_repost_ledger WHERE queue_id=?', (queue_id,))
        jobs = [dict(row) for row in c.execute("SELECT * FROM dispatch.pipeline_job WHERE source_key=? AND channel='x' AND family='queue' ORDER BY stage", (key,))]
        hold = one('SELECT * FROM pipeline_canonical_hold WHERE source_key=?', (key,))
        resources = [dict(row) for row in c.execute('''SELECT r.* FROM dispatch.pipeline_resource r
          JOIN dispatch.pipeline_job j ON j.id=r.job_id WHERE j.source_key=?''', (key,))]
        legacy = c.execute('''SELECT 1 FROM dispatch.pipeline_legacy_resource r
          JOIN dispatch.pipeline_job j ON j.id=r.job_id WHERE j.source_key=? LIMIT 1''', (key,)).fetchone()
        published = c.execute('SELECT 1 FROM dispatch.pipeline_publication WHERE source_key=?', (key,)).fetchone()
        route = one('SELECT * FROM x_post_drama_delivery_route WHERE queue_id=?', (queue_id,))
        return {'queue': q, 'log': log, 'relay': relay, 'jobs': jobs, 'hold': hold,
                'resources': resources, 'legacy': bool(legacy), 'published': bool(published), 'route': route}

    @staticmethod
    def version(s):
        # Bounded optimistic identity; heartbeats/current generations invalidate
        # an old selection, while unrelated queues cannot invalidate it.
        values = {k: s[k] for k in ('queue', 'log', 'relay', 'hold', 'resources', 'legacy', 'published')}
        values['jobs'] = [{k: j[k] for k in ('id','status','generation','external_intent','hold_reason')} for j in s['jobs']]
        return hashlib.sha256(dump(values).encode()).hexdigest()

    @staticmethod
    def reason(s, action):
        q, log, relay, jobs = (s[k] for k in ('queue','log','relay','jobs'))
        if q['status'] == 'published' or log.get('status') == 'published' or log.get('x_post_id') or log.get('x_post_url') or s['published']:
            return '已发布，不能重置或停止'
        if (log.get('unknown_outcome') or relay.get('unknown_outcome')
                or log.get('status') in {'post_creating','repost_creating','source_published'}
                or relay.get('status') in {'needs_review','reposting','source_published','reposted'}
                or relay.get('source_post_id') or relay.get('repost_id')
                or any(j['status'] == 'unknown' for j in jobs)):
            return '已有发布回执或结果待核对，请先核对原记录'
        if (q['status'] == 'publishing' or log.get('status') in {'media_uploading','publishing'}
                or relay.get('status') == 'source_publishing'
                or s['resources'] or s['legacy'] or any(j['status'] in ACTIVE for j in jobs)):
            return '队列正在执行，请等待本次处理结束后操作'
        if not jobs:
            return '队列尚未同步到调度器，请刷新后重试'
        if s['hold'] and s['hold'].get('reason') != STOP:
            return '队列有其他保留状态，需先处理原保留原因'
        if any(j['status'] in {'archived','backlog_paused'} and j['hold_reason'] != STOP for j in jobs):
            return '队列已归档或有原有暂停，需在原流程处理'
        if any(j['grandfathered'] and j['status'] not in {'failed','completed'} for j in jobs):
            return '旧执行流程仍持有此队列'
        if q['status'] not in {'queued','waiting_relay','reserved','failed','cancelled'}:
            return '当前队列状态不支持此操作'
        if log and log['status'] not in {'reserved','failed'}:
            return '当前发布记录状态不支持此操作'
        if log.get('attempt_count') and log.get('status') != 'failed':
            return '已有尝试尚未明确失败，请先核对原记录'
        if relay and relay.get('status') not in {'reserved','failed'}:
            return '转推记录尚未完成核对'
        if action == 'stop':
            if s['hold'].get('reason') == STOP:
                return '队列已停止'
            if q['status'] in {'failed','cancelled'} or all(j['status'] in {'failed','completed'} for j in jobs):
                return '队列已结束，无需停止'
        return ''

    def query(self, payload):
        from .log_query import query_logs
        try:
            with contextlib.closing(self.connect(readonly=True)) as c:
                c.execute('BEGIN')
                result = query_logs(self.post_db, payload, connection=c)
                has_audit = c.execute("SELECT 1 FROM sqlite_master WHERE name='x_post_queue_control_audit'").fetchone()
                for item in result['items']:
                    s = self.snapshot(c, item['queue_id'])
                    item['action_version'] = self.version(s)
                    for action in ('reset','stop'):
                        item[action + '_reason'] = self.reason(s, action)
                        item['can_' + action] = not item[action + '_reason']
                    if has_audit:
                        prior = c.execute("SELECT action,before_json,created_at FROM x_post_queue_control_audit WHERE queue_id=? AND action='reset' AND json_extract(result_json,'$.ok')=1 ORDER BY id DESC LIMIT 1", (item['queue_id'],)).fetchone()
                        if prior:
                            old = json.loads(prior['before_json']).get('log', {})
                            item.update(last_reset_at=prior['created_at'], previous_error_code=old.get('error_code',''), previous_attempt_count=old.get('attempt_count',0))
        except sqlite3.Error:
            result = query_logs(self.post_db, payload)
            for item in result['items']:
                item.update(can_reset=False, can_stop=False, reset_reason='调度状态暂不可用，请刷新后重试', stop_reason='调度状态暂不可用，请刷新后重试')
        return result

    def apply(self, payload, actor):
        if not isinstance(payload, dict) or set(payload) != {'operation_id','action','items'}:
            raise XPostError('invalid_request', '操作参数无效', 400)
        op, action, items = payload['operation_id'], payload['action'], payload['items']
        if not isinstance(op, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', op) or action not in {'reset','stop'}:
            raise XPostError('invalid_request', '操作编号或操作类型无效', 400)
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise XPostError('invalid_request', '一次可操作 1 至 100 条队列', 400)
        ids = []
        for item in items:
            if (not isinstance(item, dict) or set(item) != {'queue_id','version'} or type(item['queue_id']) is not int
                    or item['queue_id'] <= 0 or not isinstance(item['version'],str) or not re.fullmatch(r'[0-9a-f]{64}',item['version'])):
                raise XPostError('invalid_request', '队列或版本参数无效，请刷新后重试', 400)
            ids.append(item['queue_id'])
        if len(set(ids)) != len(ids):
            raise XPostError('invalid_request', '不能重复提交同一队列', 400)
        safe_actor = {k: str(actor.get(k) or '')[:200] for k in ('tenant_key','user_id','name')}
        if not safe_actor['user_id']:
            raise XPostError('admin_required', '管理员身份缺失', 403)
        results = []
        # One short transaction per item: a conflict/failure never stops siblings.
        for item in items:
            with contextlib.closing(self.connect()) as c:
                try:
                    c.execute('BEGIN IMMEDIATE')
                    self.install(c)
                    previous = c.execute('SELECT * FROM x_post_queue_control_audit WHERE operation_id=? AND queue_id=?', (op,item['queue_id'])).fetchone()
                    if previous:
                        if previous['action'] != action or previous['actor_json'] != dump(safe_actor):
                            raise XPostError('operation_conflict', '操作编号已被其他请求使用', 409)
                        results.append(dict(json.loads(previous['result_json']), replayed=True))
                        c.rollback()
                        continue
                    s = self.snapshot(c, item['queue_id'])
                    reason = self.reason(s, action)
                    if not reason and self.version(s) != item['version']:
                        reason = '队列状态已变化，请刷新后重新选择'
                    if reason:
                        result = {'queue_id':item['queue_id'],'ok':False,'message':reason}
                    else:
                        self._change(c, s, action)
                        result = {'queue_id':item['queue_id'],'ok':True,'message':'已重新入队，等待执行' if action=='reset' else '已停止发布'}
                    # Preserve every original failure and attempt, plus generation
                    # evidence. Never expose internal claim/worker credentials.
                    before = {k:s[k] for k in ('queue','log','relay','hold')}
                    before['jobs'] = [{k:j[k] for k in ('id','stage','status','generation','external_intent','error_code','error_message','artifact_json','canonical_receipt')} for j in s['jobs']]
                    c.execute('INSERT INTO x_post_queue_control_audit(operation_id,queue_id,action,actor_json,before_json,result_json,created_at) VALUES(?,?,?,?,?,?,?)',
                              (op,item['queue_id'],action,dump(safe_actor),dump(before),dump(result),utc_now()))
                    c.commit()
                    results.append(result)
                except XPostError as exc:
                    c.rollback()
                    results.append({'queue_id':item['queue_id'],'ok':False,'message':str(exc)})
                except sqlite3.Error:
                    c.rollback()
                    results.append({'queue_id':item['queue_id'],'ok':False,'message':'状态暂不可用，请保留此操作并重试'})
        return {'items':results,'succeeded':sum(r['ok'] for r in results),'skipped':sum(not r['ok'] for r in results)}

    @staticmethod
    def _change(c, s, action):
        q, log, relay, jobs = (s[k] for k in ('queue','log','relay','jobs'))
        now, key = utc_now(), 'x:queue:' + str(q['id'])
        if action == 'stop':
            c.execute('INSERT INTO pipeline_canonical_hold(source_key,cutoff,reason) VALUES(?,?,?)', (key,now,STOP))
            for j in jobs:
                if j['status'] not in {'completed','published'}:
                    c.execute("UPDATE dispatch.pipeline_job SET status='backlog_paused',hold_reason=?,updated_at=? WHERE id=?", (STOP,now,j['id']))
        else:
            c.execute('DELETE FROM pipeline_canonical_hold WHERE source_key=? AND reason=?', (key,STOP))
            status = 'waiting_relay' if s['route'].get('route_state') == 'waiting_relay' else 'queued'
            c.execute('UPDATE x_post_queue SET status=?,updated_at=? WHERE id=?', (status,now,q['id']))
            if log:
                c.execute("UPDATE x_post_publish_log SET status='reserved',attempt_count=0,unknown_outcome=0,x_media_id='',error_code='',error_message='',started_at='',updated_at=? WHERE id=?", (now,log['id']))
            if relay:
                c.execute("UPDATE x_post_repost_ledger SET status='reserved',source_attempt_count=0,repost_attempt_count=0,unknown_outcome=0,error_code='',error_message='',updated_at=? WHERE id=?", (now,relay['id']))
            XPostStore._sync_run(c,q['id'],now)
            if q.get('manual_run_id'):
                c.execute("UPDATE x_post_manual_run SET status='running',error_code='',error_message='',finished_at='',updated_at=? WHERE id=? AND trigger_source='auto_template' AND unknown_count=0", (now,q['manual_run_id']))
            # A finished preparation and its artifact stay finished. Only rearm
            # failed/pending phases; next claim receives a new generation.
            for j in jobs:
                if j['status'] == 'completed' and j['stage'] == 'prepare':
                    continue
                if j['stage'] == 'prepare' and any(p['stage']=='publish' and json.loads(p['artifact_json']).get('sha256') for p in jobs):
                    c.execute("UPDATE dispatch.pipeline_job SET status='completed',generation=generation+1,claim_hash='',lease_expires_at='',finished_at=?,updated_at=? WHERE id=?", (now,now,j['id']))
                    continue
                payload, meta = json.loads(j['payload_json']), json.loads(j['source_meta_json'])
                for value in (payload,meta):
                    value.update(source_attempt_count=0,reconcile_only=False,legacy_inflight=False)
                c.execute("""UPDATE dispatch.pipeline_job SET status='queued',hold_reason='',source_status='queued',
                  source_meta_json=?,payload_json=?,external_intent=0,grandfathered=0,generation=generation+1,node_id='',worker_id='',claim_hash='',
                  lease_expires_at='',started_at='',finished_at='',error_code='',error_message='',canonical_receipt='',updated_at=? WHERE id=?""",
                  (dump(meta),dump(payload),now,j['id']))
        for j in jobs:
            c.execute('INSERT INTO dispatch.pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                      (j['id'],'operator_x_queue_'+action,dump({'queue_id':q['id'],'previous_status':j['status'],'previous_generation':j['generation']}),now))


def dispatch(handler, parsed, app):
    from features.x_video_publish.errors import PublishingError
    reply = lambda status, data: app['json_response'](handler,status,data,no_store=True)
    if not handler._require_cookie_admin():
        return
    try:
        manager = LogManager()
        if handler.command == 'GET':
            params = app['x_post_admin_query_params'](parsed.query)
            return reply(200, manager.query(params))
        from features.x_video_publish.gateway import read_json
        payload = read_json(handler, limit=64 * 1024)
        if payload is None:
            return
        session = handler._session() or {}
        actor = {'tenant_key':session.get('tenant_key'),'user_id':session.get('user_id'),'name':session.get('name') or session.get('user_name')}
        return reply(200,manager.apply(payload,actor))
    except (XPostError,PublishingError,ValueError) as exc:
        return reply(getattr(exc,'status',400),{'error':getattr(exc,'code','invalid_request'),'message':str(exc)})
    except Exception:
        return reply(503,{'error':'x_queue_control_unavailable','message':'队列服务暂不可用，请刷新或使用原操作重试'})
