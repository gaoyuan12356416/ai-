"""Read-only publishing failure observer with a separate durable message outbox."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import closing, contextmanager
from urllib.parse import urlencode

import requests


PHASES = {'cover': '生成封面', 'enqueue': '提交发布', 'upload': '上传视频',
          'thumbnail': '设置封面', 'processing': '视频处理', 'public': '公开视频', 'comment': '发送首评', 'schedule':'定时发布'}


def _failure_event_fields(task_id, version, state, updated_at, error, ledger):
    """Keep the deployed observer's event identity byte-for-byte compatible."""
    preparation = state in ('generation_failed', 'enqueue_failed', 'schedule_missed')
    ledger = ledger or {}
    if not preparation and ledger.get('status') not in ('failed', 'unknown', 'partial_failed', 'schedule_missed') and ledger.get('comment_status') not in ('failed', 'unknown'):
        return None
    phase = ('schedule' if state=='schedule_missed' else 'cover' if state == 'generation_failed' else 'enqueue') if preparation else ledger.get('reviewed_phase') or 'upload'
    code = str(error.get('code', state)) if preparation else str(ledger.get('error_code') or 'publish_failed')
    message = str(error.get('message') or '任务准备失败') if preparation else str(ledger.get('error_message') or '发布阶段执行失败')
    event_time = updated_at if preparation else ledger.get('updated_at_utc')
    if not preparation and phase=='schedule':event_time=ledger.get('schedule_event_at') or event_time
    if state=='schedule_missed':
        event_time=error.get('missed_at') or event_time
        version=0  # One event per missed appointment, independent of cover reviews.
    identity = [task_id, version, phase, code, event_time]
    event_key = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    return {'event_key': event_key, 'phase': phase, 'code': code, 'message': message, 'time': event_time}


def failure_notification_status(outbox_db, body, ledger):
    """Project the existing independent outbox without creating or changing it."""
    if not outbox_db or not isinstance(body, dict) or not body.get('id'):
        return None
    try:
        path = Path(outbox_db).resolve()
        if not path.is_file():
            return None
        error = body.get('error') if isinstance(body.get('error'), dict) else {}
        current = _failure_event_fields(body['id'], body.get('current_version'), body.get('status'),
                                        body.get('updated_at'), error, ledger)
        # This optional projection must not stall task lists on a writer lock.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM failure_notice WHERE event_key=?', (current['event_key'],)).fetchone() if current else None
            if row is None:
                # Existing outboxes store task_id only in the immutable payload.
                row = db.execute('''SELECT * FROM failure_notice
                    WHERE CASE WHEN json_valid(payload) THEN json_extract(payload,'$.task_id') END = ?
                    ORDER BY created_at DESC,event_key DESC LIMIT 1''', (body['id'],)).fetchone()
            if row is None:
                return None
            payload = json.loads(row['payload'])
            if not isinstance(payload, dict):
                return None
    except (OSError, sqlite3.Error, ValueError):
        return None
    status = row['state']
    message = {'pending': '等待发送异常提醒', 'sending': '正在发送异常提醒', 'sent': '飞书异常提醒已发送',
               'failed': '异常提醒发送失败，请查看任务详情', 'unknown': '异常提醒发送结果待核查，请查看飞书',
               'superseded': '任务状态已更新，本条异常提醒未发送'}.get(status, '异常提醒状态待核查')
    return {'event_id': row['event_key'], 'status': status, 'message': message,
            'stage': payload.get('phase', ''), 'error_code': payload.get('code', ''),
            'is_current': bool(current and row['event_key'] == current['event_key'])}


class FailureNotifications:
    def __init__(self, source_db, outbox_db, sender):
        self.source_db = Path(source_db).resolve()
        self.outbox_db = Path(outbox_db).resolve()
        if self.source_db == self.outbox_db:
            raise ValueError('A separate notification outbox is required')
        self.sender = sender
        self.outbox_db.parent.mkdir(parents=True, exist_ok=True)
        with self._outbox() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS failure_notice(
                event_key TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL,
                lease_until REAL NOT NULL DEFAULT 0,message_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,sent_at REAL)''')
        os.chmod(self.outbox_db, 0o600)

    @contextmanager
    def _outbox(self):
        db = sqlite3.connect(self.outbox_db, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def candidates(self):
        # Never claim/retry/update a publish task while observing its failures.
        with closing(sqlite3.connect(self.source_db.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            columns={row[1] for row in db.execute('PRAGMA table_info(drama_youtube_publish)')}
            schedule_event='l.schedule_event_at' if 'schedule_event_at' in columns else "'' AS schedule_event_at"
            rows = db.execute('''SELECT p.id,p.version,p.body,p.state,p.updated_at,
                l.status AS publish_status,l.comment_status,l.reviewed_phase,l.error_code,
                l.error_message,l.updated_at_utc,l.video_id,'''+schedule_event+'''
                FROM youtube_auto_preparation p LEFT JOIN drama_youtube_publish l
                  ON l.preparation_id=p.id AND l.workflow='reviewed_thumbnail'
                WHERE p.state IN ('generation_failed','enqueue_failed','schedule_missed')
                   OR l.status IN ('failed','unknown','partial_failed','schedule_missed')
                   OR l.comment_status IN ('failed','unknown')
                ORDER BY p.created_at DESC LIMIT 200''').fetchall()
        result = []
        for row in rows:
            body = json.loads(row['body'])
            preparation = row['state'] in ('generation_failed', 'enqueue_failed', 'schedule_missed')
            ledger = dict(row, status=row['publish_status'])
            event = _failure_event_fields(row['id'], row['version'], row['state'], row['updated_at'], body.get('error') or {}, ledger)
            creator = body.get('creator') or {}
            result.append({'event_key': event['event_key'], 'task_id': row['id'], 'version': row['version'],
                'phase': event['phase'], 'code': event['code'], 'message': event['message'][:600], 'time': event['time'],
                'title': str(body.get('title') or '')[:160],
                'channel': str((body.get('channel') or {}).get('name') or '')[:120],
                'video_id': row['video_id'] or '',
                'unknown': not preparation and (row['publish_status'] == 'unknown' or row['comment_status'] == 'unknown'),
                'receive_id': creator.get('open_id') or creator.get('user_id') or '',
                'receive_id_type': 'open_id' if creator.get('open_id') else 'user_id'})
        return result

    def run_once(self):
        current = {item['event_key']: item for item in self.candidates()}
        with self._outbox() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE failure_notice SET state='unknown',lease_until=0 WHERE state='sending' AND lease_until<?", (time.time(),))
            for item in current.values():
                db.execute("INSERT OR IGNORE INTO failure_notice(event_key,payload,state,created_at) VALUES(?,?,'pending',?)",
                           (item['event_key'], json.dumps(item, ensure_ascii=False), time.time()))
            row = db.execute("SELECT event_key,payload FROM failure_notice WHERE state='pending' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return {'claimed': False}
            key = row['event_key']
            if key not in current:
                db.execute("UPDATE failure_notice SET state='superseded' WHERE event_key=?", (key,))
                return {'claimed': True, 'state': 'superseded'}
            db.execute("UPDATE failure_notice SET state='sending',lease_until=? WHERE event_key=?", (time.time() + 120, key))
        # A failure that has already been retried/recovered must not send a stale alert.
        try:
            still_current = {item['event_key'] for item in self.candidates()}
        except Exception:
            # No sender was invoked: this is a safe deferred read, not unknown
            # delivery. Retry the same durable key after the source is readable.
            with self._outbox() as db:
                db.execute("UPDATE failure_notice SET state='pending',lease_until=0 WHERE event_key=? AND state='sending'", (key,))
            return {'claimed': True, 'state': 'pending', 'task_id': current[key]['task_id'], 'deferred': True}
        if key not in still_current:
            state, message_id = 'superseded', ''
        else:
            try:
                message_id = self.sender(current[key])
                if not message_id:
                    raise RuntimeError('message_receipt_missing')
                state = 'sent'
            except Exception:
                # Unknown delivery is not blindly replayed. The stable Feishu UUID
                # and this persisted state protect against duplicate messages.
                state, message_id = 'unknown', ''
        with self._outbox() as db:
            db.execute('UPDATE failure_notice SET state=?,message_id=?,lease_until=0,sent_at=? WHERE event_key=?',
                       (state, str(message_id), time.time() if state == 'sent' else None, key))
        return {'claimed': True, 'state': state, 'task_id': current[key]['task_id']}


def build_failure_notifications(app):
    root = Path(os.environ.get('YOUTUBE_AUTO_STORAGE_ROOT', '/mnt/data-disk/youtube-auto-publish')).resolve()
    if str(root).startswith('/mnt/data-disk/') and not os.path.ismount('/mnt/data-disk'):
        raise RuntimeError('Publishing data mount is unavailable')

    def sender(event):
        if not event['receive_id']:
            raise RuntimeError('Failure notification recipient missing')
        phase = '上传后视频状态核对' if event['code'] == 'youtube_video_reconcile_unknown' and event['phase'] == 'thumbnail' else PHASES.get(event['phase'], event['phase'])
        text = (event['title'] + '\n频道：' + event['channel'] + '\n失败阶段：' + phase
                + '\n原因：' + event['message'] + '\n错误码：' + event['code']
                + '\n任务：' + event['task_id'] + '\n封面版本：V' + str(event['version']))
        if event['video_id']:
            text += '\n视频 ID：' + event['video_id']
        if event['unknown']:
            text += '\n执行结果待核查，系统已暂停后续步骤。'
        url = app.PUBLIC_BASE_URL.split('/drama-materials')[0].rstrip('/') + '/youtube-publish.html?task_id=' + event['task_id']
        card = {'config': {'wide_screen_mode': True},
                'header': {'template': 'red', 'title': {'tag': 'plain_text', 'content': 'YouTube 发布流程异常'}},
                'elements': [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': text}},
                             {'tag': 'action', 'actions': [{'tag': 'button', 'type': 'primary',
                                 'text': {'tag': 'plain_text', 'content': '查看失败任务'}, 'url': url}]}]}
        token = app.get_feishu_tenant_access_token()
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(app.FEISHU_MESSAGE_URL + '?' + urlencode({'receive_id_type': event['receive_id_type']}),
                headers={'Authorization': 'Bearer ' + token}, timeout=30, allow_redirects=False,
                json={'receive_id': event['receive_id'], 'msg_type': 'interactive',
                      'content': json.dumps(card, ensure_ascii=False), 'uuid': event['event_key'][:32]})
        data = response.json()
        if response.status_code != 200 or data.get('code') != 0:
            raise RuntimeError('Failure notification delivery unconfirmed')
        return str((data.get('data') or {}).get('message_id') or '')

    return FailureNotifications(app.JOB_DB_PATH, root / 'failure-notifications.sqlite3', sender)
