"""Read-only OAuth/channel preflight and safe, nonblocking picker snapshots.

YouTube has no read-only custom-thumbnail eligibility endpoint. A successful
identity probe is not proof of thumbnail permission or content acceptance.
"""
import copy
import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests

from features.drama_synthesis.core import REVIEWED_SCOPES, normalize_channel_scopes, scope_capabilities
from features.drama_synthesis.youtube import CHANNELS_URL, TOKEN_URL
from .templates import WorkflowError


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def verdict(status, reason, **extra):
    return dict(auth_status=status, eligible=status == 'verified', reason=reason, checked_at=utc_now(), **extra)


class ChannelProbe:
    def __init__(self, session_factory=requests.Session):
        self.session_factory = session_factory

    def __call__(self, credential):
        if credential.channel_status != 1:
            return verdict('blocked', '频道已停用，请联系管理员')
        if not credential.capabilities['refreshable']:
            return verdict('blocked', '缺少有效刷新凭证，请重新授权频道')
        if not credential.capabilities['eligible'] or not REVIEWED_SCOPES.intersection(credential.scopes):
            return verdict('blocked', '授权范围不足，请重新授权视频发布权限')
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.post(TOKEN_URL, data=dict(client_id=credential.client_id,
                client_secret=credential.client_secret, refresh_token=credential.refresh_token,
                grant_type='refresh_token'), timeout=(3, 8), allow_redirects=False)
            payload = response.json()
            if response.status_code != 200 or not payload.get('access_token'):
                if payload.get('error') in ('invalid_grant', 'invalid_client', 'unauthorized_client'):
                    return verdict('blocked', '频道授权已失效，请重新授权')
                return verdict('unknown', '授权校验暂不可用，请稍后重新检查')
            scopes = normalize_channel_scopes(payload) if payload.get('scope') else credential.scopes
            capabilities = scope_capabilities(scopes)
            if not REVIEWED_SCOPES.intersection(scopes) or not capabilities['identity_eligible']:
                return verdict('blocked', '当前 Token 的发布权限不足，请重新授权')
            response = session.get(CHANNELS_URL, params={'part':'id,snippet,status', 'mine':'true'},
                headers={'Authorization':'Bearer '+payload['access_token']}, timeout=(3,8), allow_redirects=False)
            data = response.json()
            if response.status_code != 200:
                return verdict('blocked' if response.status_code in (401,403) else 'unknown',
                    '无法读取授权频道，请检查权限后重新鉴权')
            items = data.get('items')
            if not isinstance(items, list) or len(items) != 1 or items[0].get('id') != credential.channel_id:
                return verdict('blocked', 'Token 对应频道与配置不一致，请重新授权正确频道')
            long_status = items[0].get('status', {}).get('longUploadsStatus', 'unknown')
            if long_status != 'allowed':
                return verdict('blocked' if long_status in ('disallowed','eligible') else 'unknown',
                    '需完成手机验证并开通频道中级功能（长视频、自定义封面）' if long_status=='eligible'
                    else '长视频功能未通过校验，请在 YouTube Studio 检查功能使用资格', long_uploads_status=long_status)
            return verdict('verified', '鉴权通过；封面和内容仍以平台实际处理结果为准',
                long_uploads_status=long_status, thumbnail_permission='unverified',
                comment_eligible=capabilities['comment_eligible'])
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
            return verdict('unknown', '鉴权请求暂时失败，请稍后重新检查')
        finally:
            session.close()


class ChannelFailures:
    """Read existing evidence; never modify the publishing or notice ledger."""
    def __init__(self, db_path, outbox_path):
        self.db_path, self.outbox_path = Path(db_path), Path(outbox_path)
        self.audit_path = self.outbox_path.with_name('channel-checks.sqlite3')

    def __call__(self):
        with closing(sqlite3.connect(self.db_path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT id,preparation_id,channel_local_id,channel_id,thumbnail_status,error_code,error_message,updated_at_utc FROM drama_youtube_publish WHERE app_id='1479' AND workflow='reviewed_thumbnail'").fetchall()
            successes = db.execute("SELECT l.channel_local_id,l.channel_id,MAX(e.created_at_utc) AS at FROM drama_youtube_publish_event e JOIN drama_youtube_publish l ON l.id=e.task_id WHERE l.app_id='1479' AND l.workflow='reviewed_thumbnail' AND e.phase='thumbnail' AND e.outcome='succeeded' GROUP BY l.channel_local_id,l.channel_id").fetchall()
        identities = {r['preparation_id']:(str(r['channel_local_id']),str(r['channel_id'])) for r in rows}
        rejected, succeeded = {}, {}
        for row in successes:succeeded[(str(row['channel_local_id']),str(row['channel_id']))]=row['at']
        for row in rows:
            key = identities[row['preparation_id']]
            if row['error_code']=='youtube_thumbnail_set_failed' and '403' in row['error_message'] and '(forbidden)' in row['error_message']:
                rejected[key] = max(rejected.get(key,''), row['updated_at_utc'])
        if self.outbox_path.is_file():
            with closing(sqlite3.connect(self.outbox_path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
                for (raw,) in db.execute("SELECT payload FROM failure_notice WHERE json_valid(payload) AND json_extract(payload,'$.code')='youtube_thumbnail_set_failed'"):
                    event = json.loads(raw)
                    key = identities.get(event.get('task_id'))
                    message = str(event.get('message',''))
                    if key and event.get('code')=='youtube_thumbnail_set_failed' and '403' in message and '(forbidden)' in message:
                        rejected[key] = max(rejected.get(key,''), str(event.get('time','')))
        if self.audit_path.is_file():
            with closing(sqlite3.connect(self.audit_path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
                for local_id,channel_id,stamp in db.execute('SELECT channel_local_id,channel_id,MAX(failure_at) FROM thumbnail_verification GROUP BY channel_local_id,channel_id'):
                    key=(local_id,channel_id)
                    succeeded[key]=max(succeeded.get(key,''),stamp)
        return {key:stamp for key,stamp in rejected.items() if stamp>succeeded.get(key,'')}

    def acknowledge(self, row, actor, failure_at):
        # Append-only human verification; never probe by setting a test thumbnail.
        self.audit_path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(str(self.audit_path),os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
        with closing(sqlite3.connect(self.audit_path,timeout=3)) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS thumbnail_verification(channel_local_id TEXT NOT NULL,channel_id TEXT NOT NULL,failure_at TEXT NOT NULL,tenant TEXT NOT NULL,operator TEXT NOT NULL,verified_at TEXT NOT NULL,PRIMARY KEY(channel_local_id,channel_id,failure_at))')
                db.execute('INSERT OR IGNORE INTO thumbnail_verification VALUES(?,?,?,?,?,?)',
                    (str(row['id']),row['channel_id'],failure_at,actor['tenant_key'],actor['user_id'],utc_now()))


class ChannelDirectory:
    def __init__(self, repository, *, probe=None, failures=lambda: {}, ttl=300, clock=time.monotonic):
        self.repository, self.probe, self.failures = repository, probe or ChannelProbe(), failures
        self.ttl, self.clock = ttl, clock
        self.lock = threading.Lock()
        self.items, self.running, self.finished = [], False, None
        self.error, self.started = '', None

    @staticmethod
    def row(credential):
        return dict(id=credential.channel_local_id, channel_id=credential.channel_id,
            youtube_account_id=credential.account_id, name=credential.channel_name, language='',
            scopes=sorted(credential.scopes), comment_eligible=False, eligible=False,
            auth_status='checking', reason='正在核验频道授权', checked_at='')

    @staticmethod
    def apply_failure(row, failures):
        key = (str(row['id']), str(row['channel_id']))
        if key in failures:
            row.update(verdict('blocked', '最近设置封面被 YouTube 拒绝（403 forbidden），需核验封面权限后恢复',
                thumbnail_permission='denied', failure_at=failures[key]))
        return row

    def options(self, actor=None, *, refresh=False):
        with self.lock:
            now = self.clock()
            due = self.finished is None or now-self.finished >= self.ttl
            # Bound forced refresh frequency as well as concurrent checks.
            if not self.running and (due or refresh) and (self.started is None or now-self.started >= 15):
                self.running, self.started, self.error = True, now, ''
                for row in self.items:
                    row.update(eligible=False, auth_status='checking', reason='正在重新核验频道授权')
                threading.Thread(target=self._refresh, daemon=True, name='youtube-channel-check').start()
            items = copy.deepcopy(self.items)
            checking, error = self.running, self.error
            if due and not checking:
                for row in items:row.update(eligible=False, auth_status='unknown', reason='鉴权结果已过期，请重新检查')
        try:
            failures = self.failures()
            items = [self.apply_failure(row, failures) for row in items]
        except Exception:
            items = [dict(row, eligible=False, auth_status='unknown', reason='发布权限记录暂不可用，请稍后重试') for row in items]
        safe = [{k:v for k,v in row.items() if k not in ('scopes','youtube_account_id')} for row in items]
        return dict(channels=safe, checking=checking, error=error, ttl_seconds=self.ttl,
            checked_at=max((r.get('checked_at','') for r in items),default=''))

    def _refresh(self):
        try:
            credentials = self.repository._query('CAST(ch.app_id AS UNSIGNED)=1479')
            latest = {}
            for credential in credentials:
                latest.setdefault(credential.channel_local_id, credential)
            rows = {key:self.row(credential) for key,credential in latest.items()}
            with self.lock:self.items = list(rows.values())
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix='youtube-channel-probe') as pool:
                pending = {pool.submit(self.probe,credential):key for key,credential in latest.items()}
                for future in as_completed(pending):
                    key = pending[future]
                    try:result = future.result()
                    except Exception:result = verdict('unknown','鉴权暂不可用，请稍后重新检查')
                    with self.lock:rows[key].update(result)
            with self.lock:self.finished = self.clock()
        except Exception:
            with self.lock:
                self.error = '频道列表更新失败，请稍后重新检查'
                self.finished = self.clock()-self.ttl+30
                for row in self.items:row.update(eligible=False,auth_status='unknown',reason=self.error)
        finally:
            with self.lock:self.running = False

    def validate(self, actor, channel_id, *, comment=False, expected=None, verify_thumbnail_at=None):
        if not re.fullmatch(r'[1-9][0-9]{0,18}', str(channel_id)):
            raise WorkflowError('channel_unavailable','请选择有效频道',409)
        # Exact selected row, live DB and network, never trust picker TTL.
        try:
            credentials = self.repository._query('CAST(ch.app_id AS UNSIGNED)=1479 AND CAST(ch.id AS UNSIGNED)='+str(channel_id))
            if not credentials:raise WorkflowError('channel_unavailable','频道已移除，请重新选择',409)
            credential = credentials[0]
            if expected and (str(credential.account_id),credential.channel_id)!=(str(expected['youtube_account_id']),expected['channel_id']):
                raise WorkflowError('channel_identity_changed','频道授权身份已变化，请重新创建任务',409)
            row = self.row(credential)
            row.update(self.probe(credential))
            failures=self.failures()
            if verify_thumbnail_at is None:
                self.apply_failure(row,failures)
            elif failures.get((str(row['id']),row['channel_id'])) not in (None,verify_thumbnail_at):
                raise WorkflowError('channel_check_conflict','频道出现了新的封面失败，请刷新后重新核验',409)
        except WorkflowError:raise
        except Exception:
            raise WorkflowError('channel_check_unavailable','频道鉴权暂不可用，请稍后重试',503) from None
        if row['eligible'] is not True:
            raise WorkflowError('channel_unavailable',row['reason'],409)
        if comment and not row.get('comment_eligible'):
            raise WorkflowError('comment_scope_required','该频道尚未获得首评权限',409)
        return row

    def verify_thumbnail(self, actor, payload):
        if actor.get('role')!='admin':raise WorkflowError('forbidden','仅管理员可确认频道功能核验',403)
        if payload.get('confirmation')!='studio_thumbnail_verified' or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',str(payload.get('failure_at',''))):
            raise WorkflowError('confirmation_required','请先在该频道 YouTube Studio 确认自定义缩略图可用',400)
        row=self.validate(actor,str(payload.get('channel_id','')),verify_thumbnail_at=payload['failure_at'])
        current=self.failures().get((str(row['id']),row['channel_id']))
        if current is not None:
            if current!=payload['failure_at']:raise WorkflowError('channel_check_conflict','存在更新的失败，请刷新后核验',409)
            self.failures.acknowledge(row,actor,payload['failure_at'])
        return {'verified':True,'message':'已记录人工功能核验，请刷新频道鉴权状态'}
