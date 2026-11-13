"""Immutable per-publication short links with promotion identity and durable outbox."""
from contextlib import contextmanager
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit

from .db import _utc_datetime
from .promotions import identity, now, PromotionError


SHORT_COLUMNS = ('short_link_id', 'institution_user_id', 'project_type', 'promotion_key',
    'promotion_request_id', 'koc_code', 'story_id', 'tenant_key', 'user_id', 'star_user_id',
    'platform', 'account_id', 'task_id', 'material_id', 'purpose', 'short_code', 'short_url',
    'target_url', 'status', 'created_at', 'updated_at')


def promotion_key(institution, star, story):
    return hashlib.sha256((institution+':'+star+':2:'+str(story)).encode('utf-8')).hexdigest()


def safe_target(value):
    try:
        parsed = urlsplit(str(value or ''))
        host = (parsed.hostname or '').lower()
        return (parsed.scheme == 'https' and (host == 'moboreels.com' or host.endswith('.moboreels.com'))
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not any(ord(c) < 32 or ord(c) == 127 for c in str(value)))
    except ValueError:
        return False


class ShortLinks:
    def __init__(self, settings, db, promotions):
        self.settings, self.db, self.promotions = settings, db, promotions
        self.path = settings.data_dir / 'short-links.sqlite3'
        self.mirror_lock = threading.Lock()
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS links (short_link_id TEXT PRIMARY KEY, short_code TEXT UNIQUE NOT NULL, record_json TEXT NOT NULL, mirror_pending INTEGER NOT NULL DEFAULT 1)')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL'); conn.execute('PRAGMA synchronous=FULL')
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _get(conn, key):
        row = conn.execute('SELECT record_json FROM links WHERE short_link_id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(conn, record):
        conn.execute('INSERT INTO links(short_link_id,short_code,record_json,mirror_pending) VALUES(?,?,?,1) ON CONFLICT(short_link_id) DO UPDATE SET record_json=excluded.record_json,mirror_pending=1',
                     (record['short_link_id'], record['short_code'], json.dumps(record, ensure_ascii=False, sort_keys=True)))

    def resolve(self, body):
        actor = body.get('actor') or {}
        star, tenant, user, name = identity(self.settings.institution_user_id, actor)
        values = {}
        for field, limit in [('story_id',64), ('platform',20), ('account_id',128), ('task_id',255), ('material_id',128), ('purpose',32)]:
            value = str(body.get(field) or '').strip()
            if not value or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError('推广关联字段无效: '+field)
            values[field] = value
        if values['platform'] not in ('x','fb','tt','youtube'):
            raise ValueError('不支持的社媒渠道')
        stable = [self.settings.institution_user_id, tenant, user]+[values[k] for k in ('platform','account_id','task_id','material_id','purpose')]
        key = hashlib.sha256(json.dumps(stable, ensure_ascii=False, separators=(',',':')).encode()).hexdigest()
        with self.connect() as c:
            existing = self._get(c, key)
        if existing and existing['story_id'] != values['story_id']:
            raise PromotionError('已冻结任务的短剧 ID 不能改变', 'short_link_conflict', 409)
        if existing and existing['status'] == 'succeeded':
            return self.public(existing)
        drama = self.db.get_drama(values['story_id'])
        if not drama or int(drama.get('is_show') or 0) != 1:
            raise PromotionError('短剧不存在或已下架，不能创建新的推广短链；已生成短链仍可查看', 'drama_unavailable', 409)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            record = self._get(c, key)
            if record and record['story_id'] != values['story_id']:
                raise PromotionError('已冻结任务的短剧 ID 不能改变', 'short_link_conflict', 409)
            if record and record['status'] == 'succeeded':
                c.commit(); return self.public(record)
            if record is None:
                code = secrets.token_urlsafe(12)
                record = dict(values, short_link_id=key, institution_user_id=self.settings.institution_user_id,
                    project_type=2, promotion_key=promotion_key(self.settings.institution_user_id,star,values['story_id']),
                    promotion_request_id=None, koc_code=None, tenant_key=tenant, user_id=user, star_user_id=star,
                    short_code=code, short_url='https://ai.yingliangads.com/m/'+code, target_url=None,
                    status='preparing', created_at=now(), updated_at=now())
                self._save(c, record)
            c.commit()
        # PromotionManager preserves intent, successful receipts and unknown-result fences.
        self.promotions.create(actor, values['story_id'])
        _, promotion = self.promotions._read_record(actor, values['story_id'])
        if not promotion:
            raise PromotionError('推广请求记录暂不可用')
        status = promotion['status']
        if status == 'succeeded' and not safe_target(promotion.get('deep_link')):
            raise PromotionError('推广目标地址未经验证，已保留回执供核对', 'invalid_promotion_target', 409)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            record = self._get(c, key)
            if record['status'] != 'succeeded':
                record.update(promotion_request_id=promotion['request_id'], koc_code=promotion.get('koc_code'),
                              target_url=promotion.get('deep_link') if status=='succeeded' else None,
                              status=status, updated_at=now())
                self._save(c, record)
            c.commit()
        if record['status'] != 'succeeded':
            raise PromotionError('推广请求结果待核对，未创建可用短链；不会重复创建未知推广',
                                 'promotion_'+record['status'], 409)
        return self.public(record)

    @staticmethod
    def public(record):
        return {k: record.get(k) for k in ('short_link_id','short_url','promotion_key','promotion_request_id','koc_code','story_id','target_url','status')}

    def target(self, code):
        if not re.fullmatch(r'[A-Za-z0-9_-]{16}', code):
            return None
        with self.connect() as c:
            row = c.execute('SELECT record_json FROM links WHERE short_code=?',(code,)).fetchone()
        record = json.loads(row[0]) if row else {}
        target = record.get('target_url')
        return target if record.get('status')=='succeeded' and safe_target(target) else None

    def flush_outbox(self):
        if not self.mirror_lock.acquire(blocking=False):
            return
        try:
            with self.connect() as c:
                rows = c.execute('SELECT short_link_id,record_json FROM links WHERE mirror_pending=1 LIMIT 20').fetchall()
            for row in rows:
                record = json.loads(row['record_json'])
                values = tuple(_utc_datetime(record[k]) if k in ('created_at','updated_at') else record.get(k) for k in SHORT_COLUMNS)
                sql = 'INSERT INTO ads_ai.cdreader_koc_short_link ('+','.join('`'+k+'`' for k in SHORT_COLUMNS)+') VALUES ('+','.join(['%s']*len(SHORT_COLUMNS))+') ON DUPLICATE KEY UPDATE '+','.join('`'+k+'`=IF(VALUES(updated_at)>=updated_at,VALUES(`'+k+'`),`'+k+'`)' for k in ('promotion_request_id','koc_code','target_url','status','updated_at'))
                try:
                    self.db._write(sql, values)
                except Exception as exc:
                    logging.warning('Moboreels short link outbox deferred: %s', type(exc).__name__)
                    return
                with self.connect() as c:
                    c.execute('UPDATE links SET mirror_pending=0 WHERE short_link_id=? AND record_json=?', (row['short_link_id'],row['record_json']))
        finally:
            self.mirror_lock.release()
