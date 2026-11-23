"""First-party Moboreels redirect counts. Never persist visitor identifiers or raw headers."""
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
import logging
import re
import sqlite3
import threading
from urllib.parse import urlsplit


BEIJING = timezone(timedelta(hours=8))
AUTOMATION = re.compile(
    r'bot(?:[ /;:)\d_-]|$)|crawler|spider|facebookexternalhit|facebot|meta-external'
    r'|preview|unfurl|headlesschrome|lighthouse|curl/|wget/|python[-/]|go-http-client'
    r'|whatsapp/|skypeuripreview|larkurl|slack-imgproxy|urlscan', re.I)
SITES = {
    'x': ('t.co', 'x.com', 'twitter.com'),
    'fb': ('facebook.com', 'fb.com', 'fb.me', 'messenger.com'),
    'tt': ('tiktok.com', 'tiktokv.com'),
    'youtube': ('youtube.com', 'youtu.be', 'youtube-nocookie.com'),
    'instagram': ('instagram.com',),
    'search': ('google.com', 'bing.com', 'baidu.com', 'duckduckgo.com'),
}
APPS = {
    'x': ('twitter for iphone', 'twitter for ipad', 'twitterandroid', 'twitter for android'),
    'fb': ('fban/', 'fbav/'), 'tt': ('musical_ly', 'tiktok/'),
    'youtube': ('com.google.ios.youtube', 'com.google.android.youtube'),
    'instagram': ('instagram ', 'instagram/'),
}
GROUPS = {
    'platform_publisher': ('platform', 'tenant_key', 'user_id'),
    'platform': ('platform',), 'publisher': ('tenant_key', 'user_id'),
    'referrer': ('referrer',), 'day': ('day',), 'account': ('platform', 'account_id'),
    'story': ('story_id',), 'material': ('material_id',),
}
METRICS = """COALESCE(SUM(clicks), 0) AS clicks, COALESCE(SUM(automated), 0) AS automated,
             COUNT(DISTINCT CASE WHEN clicks > 0 THEN short_code END) AS links_clicked"""
UPSERT = """INSERT INTO daily(day, short_code, referrer, clicks, automated) VALUES(?,?,?,?,?)
            ON CONFLICT(day, short_code, referrer) DO UPDATE SET
            clicks=clicks+excluded.clicks, automated=automated+excluded.automated"""


def traffic_source(referer, agent):
    try:
        url = urlsplit(referer or '')
        host = (url.hostname or '').lower().rstrip('.') if url.scheme in ('http', 'https') else ''
    except ValueError:
        host = ''
    if host:
        for source, domains in SITES.items():
            if any(host == d or host.endswith('.' + d) for d in domains):
                return source
        return 'other'
    ua = (agent or '').lower()
    for source, markers in APPS.items():
        if any(marker in ua for marker in markers):
            return source
    return 'unknown'


def traffic_bucket(when, referer, agent, purpose=''):
    automatic = bool(AUTOMATION.search(agent or '') or
                     re.search('prefetch|prerender|preview', purpose, re.I))
    return (when.astimezone(BEIJING).date().isoformat(), traffic_source(referer, agent),
            int(not automatic), int(automatic))


def publication(record):
    return record.get('purpose') not in ('validation', 'preview', 'test')


class ClickStats:
    def __init__(self, settings):
        self.directory = settings.data_dir.resolve()
        self.path = self.directory / 'clicks.sqlite3'
        self.institution = settings.institution_user_id
        self.ready = False
        self.lock = threading.Lock()

    @contextmanager
    def connect(self, timeout=4):
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=rwc', uri=True,
                                     timeout=timeout, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            if not self.ready:
                with self.lock:
                    if not self.ready:
                        conn.execute('PRAGMA journal_mode=WAL')
                        conn.execute('''CREATE TABLE IF NOT EXISTS daily(
                            day TEXT NOT NULL, short_code TEXT NOT NULL, referrer TEXT NOT NULL,
                            clicks INTEGER NOT NULL, automated INTEGER NOT NULL,
                            PRIMARY KEY(day,short_code,referrer)) WITHOUT ROWID''')
                        conn.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                        conn.execute("INSERT OR IGNORE INTO metadata VALUES('coverage_from',?)",
                                     (datetime.now(BEIJING).date().isoformat(),))
                        self.ready = True
            yield conn

    def record_redirect(self, record, headers):
        if not publication(record):
            return
        try:
            purpose = ' '.join(headers.get(key, '') for key in ('Sec-Purpose', 'Purpose', 'X-Purpose', 'X-Moz'))
            day, source, clicks, automated = traffic_bucket(
                datetime.now(BEIJING), headers.get('Referer'), headers.get('User-Agent'), purpose)
            with self.connect(timeout=1) as conn:
                conn.execute(UPSERT, (day, record['short_code'], source, clicks, automated))
        except Exception as exc:
            # Headers have already been sent. Avoid logging the URL, visitor or link identity.
            logging.error('Moboreels click recording failed: %s', type(exc).__name__)

    def query(self, params, actor):
        today = datetime.now(BEIJING).date()
        start = date.fromisoformat(str(params.get('date_from') or today - timedelta(days=6)))
        end = date.fromisoformat(str(params.get('date_to') or today))
        if not start <= end <= today or (end - start).days >= 31:
            raise ValueError('点击日期范围必须在31天内且不晚于北京时间今天')
        group = params.get('group_by', 'platform_publisher')
        if group not in GROUPS:
            raise ValueError('点击分组无效')
        page, size = int(params.get('page_index', 1)), int(params.get('page_size', 50))
        if not 1 <= page <= 1000000 or not 1 <= size <= 100:
            raise ValueError('点击分页无效')
        admin = actor.get('is_admin') is True
        fields = GROUPS[group]
        dimensions = ', '.join(fields)
        where = ['institution=?']
        args = [start.isoformat(), end.isoformat(), self.institution]
        if not admin:
            where += ['tenant_key=?', 'user_id=?']
            args += [actor['tenant_key'], actor['user_id']]
        platform = params.get('platform') or ''
        if platform:
            if platform not in ('x', 'fb', 'tt', 'youtube'):
                raise ValueError('发布渠道无效')
            where.append('platform=?')
            args.append(platform)
        # The immutable code is the join key, even when many posts share one KOC promotion.
        link_fields = ('institution_user_id', 'tenant_key', 'user_id', 'platform', 'account_id',
                       'story_id', 'material_id', 'star_user_id', 'publisher_name')
        projection = ', '.join("json_extract(l.record_json, '$.%s') AS %s" %
                               (k, 'institution' if k == 'institution_user_id' else k) for k in link_fields)
        scoped = f"""WITH attributed AS (
            SELECT d.*, {projection} FROM daily d JOIN frozen.links l ON l.short_code=d.short_code
            WHERE d.day BETWEEN ? AND ?
        ), scoped AS (SELECT * FROM attributed WHERE {' AND '.join(where)}) """
        select_fields = dimensions
        source = 'scoped'
        with self.connect() as conn:
            conn.execute('ATTACH DATABASE ? AS frozen', ((self.directory / 'short-links.sqlite3').as_uri()+'?mode=ro',))
            if 'user_id' in fields:
                conn.execute('ATTACH DATABASE ? AS names', ((self.directory / 'promotions.sqlite3').as_uri()+'?mode=ro',))
                source += """ LEFT JOIN names.promotions p ON
                    p.identity_key = institution || ':' || star_user_id || ':2:' || story_id"""
                select_fields += ", COALESCE(MAX(NULLIF(publisher_name,'')), MAX(NULLIF(json_extract(p.record_json,'$.star_name'),'')), user_id) AS publisher_name"
            conn.execute('BEGIN')
            coverage = conn.execute("SELECT value FROM metadata WHERE key='coverage_from'").fetchone()[0]
            summary = dict(conn.execute(scoped+'SELECT '+METRICS+' FROM scoped', args).fetchone())
            total = conn.execute(scoped+f'SELECT COUNT(*) FROM (SELECT 1 FROM scoped GROUP BY {dimensions})', args).fetchone()[0]
            order = 'day DESC' if group == 'day' else f'clicks DESC, automated DESC, {dimensions}'
            rows = conn.execute(scoped+f'SELECT {select_fields}, {METRICS} FROM {source} GROUP BY {dimensions} '
                                f'ORDER BY {order} LIMIT ? OFFSET ?', args+[size, (page-1)*size]).fetchall()
        return {'view': 'clicks', 'timezone': 'Asia/Shanghai', 'coverage_from': coverage,
                'date_from': start.isoformat(), 'date_to': end.isoformat(), 'group_by': group,
                'scope': {'kind': 'institution' if admin else 'owner', 'label': '机构全部' if admin else '本人推广'},
                'summary': summary, 'list': [dict(row) for row in rows], 'total': total,
                'page_index': page, 'page_size': size}
