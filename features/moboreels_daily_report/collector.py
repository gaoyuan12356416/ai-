"""Reuse the Mobo financial projection; count only matching native receipts.

Do not instantiate Application/Reports/ClickStats: their constructors may recover
jobs or initialize writable databases. Every local source connection is mode=ro.
"""
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
import json
import re
import sqlite3

from features.moboreels_library.clicks import BEIJING, publication
from features.moboreels_library.db import Database, _literal
from features.moboreels_library.effects import Effects, read_records
from features.moboreels_library.orders import MONEY_METRICS, COUNT_METRICS
from features.moboreels_library.promotions import publisher_identity

X_DATABASES = ('/var/lib/x-post-automation/accounts.sqlite3',
               '/mnt/data-disk/x-tag-publisher/posts.sqlite3')
FB_DATABASE = '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'
CHANNELS = {'fb': 'Facebook', 'x': 'X', 'youtube': 'YouTube', 'tt': 'TikTok',
            'shared': '多渠道共用', 'unknown': '渠道待归属'}
NUMBERS = COUNT_METRICS + ('clicks', 'published_count')


@contextmanager
def read_only(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro',
                                uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        yield conn


class ReadOnlyReports:
    def __init__(self, settings):
        self.settings = settings
        self.store = SimpleNamespace(settings=settings, reader=Database(settings),
            scope=lambda: 'institution_user_id=%s AND project_type=2' % _literal(settings.institution_user_id))

    def _jobs(self):
        with read_only(self.settings.data_dir / 'reports.sqlite3') as conn:
            return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM report_run ORDER BY rowid DESC')]


class ReadOnlyClicks:
    def __init__(self, path):
        self.path = path

    def connect(self):
        return read_only(self.path)


def utc_day(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc).date().isoformat()
    except (ValueError, TypeError):
        return None


def _batches(values):
    values = sorted(set(values))
    for offset in range(0, len(values), 400):
        yield values[offset:offset + 400]


def link_in_post(row, link):
    url = str(link.get('short_url') or '')
    return bool(url and (row.get('short_url') == url or re.search(
        re.escape(url) + r'(?![A-Za-z0-9_/?#&=%-])', str(row.get('post_text') or ''))))


def collect_publications(links, day, x_paths=X_DATABASES, fb_path=FB_DATABASE):
    """IDs alone are insufficient: account, material and frozen URL must match."""
    pending = [r for r in links if r.get('purpose') == 'post'
               and (utc_day(r.get('created_at')) or day) <= day]
    facts, issues, seen = [], set(), {}
    native = {}
    x_ids = [int(r['task_id'].split(':')[1]) for r in pending
             if re.fullmatch(r'x-queue:\d+', str(r.get('task_id', '')))]
    fb_ids = [int(r['task_id'].split(':')[2]) for r in pending
              if re.fullmatch(r'fb:auto:\d+', str(r.get('task_id', '')))]
    if x_ids:
        for path in x_paths:
            try:
                with read_only(path) as conn:
                    for ids in _batches(x_ids):
                        sql = '''SELECT l.queue_id,l.account_id,l.status,l.unknown_outcome,
                            l.x_post_id AS post_id,l.published_at AS published_at,l.short_url,l.post_text,
                            q.material_id FROM x_post_publish_log l JOIN x_post_queue q ON q.id=l.queue_id
                            WHERE l.queue_id IN (%s)''' % ','.join('?' for _ in ids)
                        for row in conn.execute(sql, ids):
                            native.setdefault('x-queue:' + str(row['queue_id']), []).append(dict(row))
            except (sqlite3.Error, OSError):
                issues.add('X 发布回执来源未完整读取')
    if fb_ids:
        try:
            with read_only(fb_path) as conn:
                for ids in _batches(fb_ids):
                    sql = '''SELECT id,page_id AS account_id,status,unknown_outcome,
                        graph_post_id AS post_id,completed_at_utc AS published_at,short_url,material_id,
                        message_text AS post_text
                        FROM fb_auto_task WHERE id IN (%s)''' % ','.join('?' for _ in ids)
                    for row in conn.execute(sql, ids):
                        native.setdefault('fb:auto:' + str(row['id']), []).append(dict(row))
        except (sqlite3.Error, OSError):
            issues.add('Facebook 发布回执来源未完整读取')
    for link in pending:
        key = str(link.get('task_id') or '')
        if not re.fullmatch(r'(x-queue:\d+|fb:auto:\d+)', key):
            issues.add(CHANNELS.get(link.get('platform'), '未知渠道') + '存在尚未接入的发布回执类型')
            continue
        matches = [r for r in native.get(key, [])
                   if str(r['account_id']) == str(link.get('account_id'))
                   and str(r['material_id']) == str(link.get('material_id'))
                   and link_in_post(r, link)]
        if not matches:
            if any(r['status'] == 'published' and utc_day(r['published_at']) == day for r in native.get(key, [])):
                issues.add(CHANNELS.get(link.get('platform'), '未知渠道') + '存在待核对的任务与短链关联')
            continue
        for row in matches:
            if row['status'] != 'published' or row['unknown_outcome']:
                continue
            if not re.fullmatch(r'\d+(?:_\d+)?', str(row['post_id'] or '')) or not utc_day(row['published_at']):
                issues.add('成功发布回执缺少有效平台 ID 或时间')
                continue
            if utc_day(row['published_at']) != day:
                continue
            post_key = (link['platform'], str(link['account_id']), row['post_id'])
            owner = (link['tenant_key'], link['user_id'])
            if post_key in seen:
                if seen[post_key] != owner:
                    issues.add('个别 Post 的冻结发布人冲突，已从发布数量排除')
                    facts = [f for f in facts if (f['platform'], f['account_id'], f['post_id']) != post_key]
                    seen[post_key] = None
                continue
            seen[post_key] = owner
            facts.append(dict(platform=link['platform'], tenant_key=owner[0], user_id=owner[1],
                              account_id=str(link['account_id']),
                              post_id=row['post_id'], published_at=row['published_at']))
    return facts, sorted(issues)


def blank():
    return {**{k: Decimal(0) for k in MONEY_METRICS}, **{k: 0 for k in NUMBERS}}


def add(target, value):
    for key in MONEY_METRICS:
        target[key] += Decimal(str(value.get(key) or '0'))
    for key in NUMBERS:
        target[key] += int(value.get(key) or 0)


def serialize(value):
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [serialize(v) for v in value]
    return value


def assemble(day, effects, links, publications, warnings, institution):
    people = {}
    def person(pid, name):
        return people.setdefault(pid, dict(publisher=pid, name=name, platforms={}, **blank()))
    def channel(p, platform):
        return p['platforms'].setdefault(platform, blank())
    for link in links:
        pid = publisher_identity(institution, link)
        p = person(pid, link.get('publisher_name') or link.get('user_id') or '发布人待归属')
        channel(p, link.get('platform') or 'unknown')
    for row in (effects or {}).get('list', []):
        dims = {d['key']: d for d in row['dimensions']}
        p = person(dims['publisher']['value'], dims['publisher']['label'])
        # The financial projection resolves the provider's task ID to real owner.
        p['name'] = dims['publisher']['label']
        add(channel(p, dims['platform']['value']), row)
    for fact in publications:
        pid = publisher_identity(institution, fact)
        channel(person(pid, fact['user_id']), fact['platform'])['published_count'] += 1
    totals, platforms = blank(), {}
    for p in people.values():
        for platform, value in p['platforms'].items():
            add(p, value)
            add(totals, value)
            add(platforms.setdefault(platform, blank()), value)
    if effects:
        for metric in MONEY_METRICS + COUNT_METRICS + ('clicks',):
            if Decimal(str(totals[metric])) != Decimal(str(effects['summary'][metric])):
                raise ValueError('daily_report_totals_not_conserved')
    quality = dict(financial_available=effects is not None,
                   orders_synced=bool(effects and effects['complete_order_days'] == 1),
                   ads_synced=bool(effects and effects['complete_ad_days'] == 1),
                   clicks_covered=bool(effects and effects.get('click_coverage_from', '9999') <= day),
                   publications_complete=not any(not w.startswith('收益查询未完成') for w in warnings))
    notices = list(warnings)
    if not quality['orders_synced']:
        notices.append('充值／退款数据未完整同步，金额仅为已读取部分，不代表零收入')
    if not quality['ads_synced']:
        notices.append('广告收入未完整同步，金额仅为已读取部分，不代表零收入')
    if not quality['clicks_covered']:
        notices.append('点击数据未完整覆盖该日')
    if totals['unclassified_order_count'] or totals['unmatched_order_count'] or totals['undated_order_count']:
        notices.append('存在待分类、待归属或缺少来源日期的订单，请在后台核对')
    quality['complete'] = all(quality.values()) and not notices
    return serialize(dict(report_date=day, generated_at=datetime.now(BEIJING).isoformat(),
        rows=sorted(people.values(), key=lambda r: (-r['total_revenue_usd'], -r['published_count'], r['name'])),
        totals=totals, platforms=platforms, quality=quality, warnings=notices,
        source_updated_at=(effects or {}).get('last_success_at'),
        financial_queried_at=(effects or {}).get('financial_queried_at'),
        publication_timezone='UTC', click_timezone='Asia/Shanghai', income_timezone='provider_unspecified'))


def collect(day, settings):
    date.fromisoformat(day)
    if not settings.institution_user_id:
        raise ValueError('institution_not_configured')
    with localcontext() as context:
        context.prec = 65
        links = [r for r in read_records(settings.data_dir / 'short-links.sqlite3', 'links')
                 if r.get('institution_user_id') == settings.institution_user_id
                 and r.get('project_type') == 2 and r.get('status') == 'succeeded' and publication(r)]
        names = {publisher_identity(settings.institution_user_id, r): r.get('star_name') for r in
                 read_records(settings.data_dir / 'promotions.sqlite3', 'promotions')
                 if r.get('institution_user_id') == settings.institution_user_id and r.get('project_type') == 2
                 and r.get('status') == 'succeeded' and r.get('star_name')}
        for link in links:
            if not link.get('publisher_name'):
                link['publisher_name'] = names.get(publisher_identity(settings.institution_user_id, link), '')
        publications, warnings = collect_publications(links, day)
        try:
            effects = Effects(settings, ReadOnlyReports(settings), ReadOnlyClicks(settings.data_dir / 'clicks.sqlite3')).query(
                dict(date_from=day, date_to=day, group_by='publisher,platform'),
                dict(tenant_key='system', user_id='moboreels-daily-report', name='Mobo 收入日报', is_admin=True))
        except Exception as exc:
            effects = None
            warnings.append('收益查询未完成：' + type(exc).__name__)
        return assemble(day, effects, links, publications, warnings, settings.institution_user_id)
