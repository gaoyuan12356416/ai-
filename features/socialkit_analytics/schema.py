"""Shared facts, exact identifiers, and SQLite snapshot schema (Python 3.9)."""
import hashlib
import json

COMMON_FIELDS = (
    'date', 'team_id', 'product', 'platform', 'account', 'account_name',
    'external_account_id', 'drama', 'drama_name', 'drama_app_id', 'drama_content_id',
)
EFFECT_FIELDS = COMMON_FIELDS + (
    'site_id', 'short_code', 'country', 'device_os', 'material_type',
    'creation_source', 'matched', 'clicks', 'views', 'installs', 'conversions',
    'revenue_cents', 'refund_cents', 'dnu_revenue_cents', 'dnu_conversions',
)
PUBLICATION_FIELDS = COMMON_FIELDS + (
    'task_id', 'language', 'account_country', 'reason', 'task_status',
    'planned', 'success', 'failed', 'tasks',
)
EFFECT_METRICS = (
    'clicks', 'views', 'installs', 'conversions', 'revenue_cents', 'refund_cents',
    'dnu_revenue_cents', 'dnu_conversions',
)
PUBLICATION_METRICS = ('planned', 'success', 'failed', 'tasks')
INTEGER_FIELDS = frozenset(EFFECT_METRICS + PUBLICATION_METRICS + (
    'team_id', 'drama_app_id', 'matched', 'task_id', 'task_status',
))
PLATFORM_LINK = {0: 'generic', 1: 'tiktok', 2: 'facebook', 3: 'instagram', 4: 'youtube', 5: 'x'}
PLATFORM_ACCOUNT = {1: 'facebook', 2: 'youtube', 3: 'tiktok', 4: 'instagram', 5: 'x'}
PRODUCTS = {1479: 'Dramawave', 3302: 'HotDrama'}
CREATION_SOURCES = {1: 'automatic_material', 2: 'ranking_drama', 3: 'browser_api'}
MATERIAL_TYPES = {0: 'ordinary', 1: 'new', 2: 'hit', 3: 'historical_hit', 4: 'old'}
TABLES = {'effects': 'socialkit_effect_daily', 'publications': 'socialkit_publish_daily'}


def stable_key(*parts):
    return json.dumps(list(parts), ensure_ascii=False, separators=(',', ':'))


def row_key(kind, row):
    fields = ('date', 'site_id', 'short_code', 'country', 'device_os') if kind == 'effects' else ('date', 'task_id')
    return hashlib.sha256(stable_key(*(row[f] for f in fields)).encode('utf-8')).hexdigest()


def empty_common(day):
    return dict(date=day, team_id=0, product='', platform='unattributed', account='',
                account_name='', external_account_id='', drama='', drama_name='',
                drama_app_id=0, drama_content_id='')


def create_snapshot_schema(conn):
    for kind, fields in (('effects', EFFECT_FIELDS), ('publications', PUBLICATION_FIELDS)):
        definitions = ['row_key TEXT PRIMARY KEY']
        definitions += ['"%s" %s NOT NULL' % (f, 'INTEGER' if f in INTEGER_FIELDS else 'TEXT') for f in fields]
        conn.execute('CREATE TABLE IF NOT EXISTS %s (%s)' % (kind, ','.join(definitions)))
        for suffix, columns in (('date', 'date'), ('platform', 'date,platform'),
                                ('product', 'date,product'), ('account', 'date,account'),
                                ('drama', 'date,drama')):
            conn.execute('CREATE INDEX IF NOT EXISTS idx_%s_%s ON %s(%s)' % (kind, suffix, kind, columns))
    conn.execute('CREATE INDEX IF NOT EXISTS idx_effect_country ON effects(date,country,device_os)')
    conn.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
