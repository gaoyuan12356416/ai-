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
          'thumbnail': '设置封面', 'processing': '视频处理', 'public': '公开视频', 'comment': '发送首评'}


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
            rows = db.execute('''SELECT p.id,p.version,p.body,p.state,p.updated_at,
                l.status AS publish_status,l.comment_status,l.reviewed_phase,l.error_code,
                l.error_message,l.updated_at_utc,l.video_id
                FROM youtube_auto_preparation p LEFT JOIN drama_youtube_publish l
                  ON l.preparation_id=p.id AND l.workflow='reviewed_thumbnail'
                WHERE p.state IN ('generation_failed','enqueue_failed')
                   OR l.status IN ('failed','unknown','partial_failed')
                   OR l.comment_status IN ('failed','unknown')
                ORDER BY p.created_at DESC LIMIT 200''').fetchall()
        result = []
        for row in rows:
            body = json.loads(row['body'])
            preparation = row['state'] in ('generation_failed', 'enqueue_failed')
            phase = ('cover' if row['state'] == 'generation_failed' else 'enqueue') if preparation else row['reviewed_phase'] or 'upload'
            error = body.get('error') or {}
            code = str(error.get('code', row['state'])) if preparation else str(row['error_code'] or 'publish_failed')
            message = str(error.get('message') or '任务准备失败') if preparation else str(row['error_message'] or '发布阶段执行失败')
            event_time = row['updated_at'] if preparation else row['updated_at_utc']
            identity = [row['id'], row['version'], phase, code, event_time]
            event_key = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
            creator = body.get('creator') or {}
            result.append({'event_key': event_key, 'task_id': row['id'], 'version': row['version'],
                'phase': phase, 'code': code, 'message': message[:600], 'time': event_time,
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
        if key not in {item['event_key'] for item in self.candidates()}:
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
