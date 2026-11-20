"""Read-only social effects. Revenue is counted once per source promotion/day.

A shared provider promotion cannot identify the converting post. Preserve that
ambiguity as a dimension value; never fan its revenue out across tracked links.
"""
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import json
import sqlite3

from .clicks import BEIJING, publication
from .db import _literal
from .orders import MONEY_METRICS, COUNT_METRICS, _period, _query_report
from .promotions import identity


DIMENSIONS = ('platform', 'publisher', 'story', 'promotion', 'account', 'day')
PLATFORMS = {'x': 'X', 'fb': 'Facebook', 'tt': 'TikTok', 'youtube': 'YouTube',
             'shared': '多渠道共用', 'unknown': '渠道待归属'}


def blank():
    return dict({k: Decimal(0) for k in MONEY_METRICS},
                **{k: 0 for k in COUNT_METRICS}, clicks=0, automated=0, _links=set())


def add(target, row):
    for name in MONEY_METRICS:
        target[name] += Decimal(str(row.get(name) or 0))
    for name in COUNT_METRICS + ('clicks', 'automated'):
        target[name] += int(row.get(name) or 0)
    target['_links'].update(row.get('_links', ()))


def public(row):
    result = {k: v for k, v in row.items() if k != '_links'}
    result.update({k: format(row[k], 'f') for k in MONEY_METRICS})
    result['links_clicked'] = len(row['_links'])
    return result


def read_records(path, table):
    # Table names are constants supplied only by this module.
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        return [json.loads(row[0]) for row in conn.execute('SELECT record_json FROM ' + table)]


class Effects:
    def __init__(self, settings, reports, clicks):
        self.settings, self.reports, self.clicks = settings, reports, clicks

    def query(self, params, actor):
        with localcontext() as context:
            context.prec = 65
            return self._query(params, actor)

    def _query(self, params, actor):
        today = datetime.now(BEIJING).date()
        period = {'date_from': params.get('date_from') or str(today - timedelta(days=6)),
                  'date_to': params.get('date_to') or str(today)}
        start, end = _period(period, today)
        dimensions = str(params.get('group_by') or 'platform,publisher').split(',')
        if not 1 <= len(dimensions) <= 3 or len(set(dimensions)) != len(dimensions) or any(d not in DIMENSIONS for d in dimensions):
            raise ValueError('请选择 1 至 3 个不同的分组维度')
        platform = str(params.get('platform') or '')
        if platform and platform not in PLATFORMS:
            raise ValueError('发布渠道无效')
        publisher = str(params.get('publisher') or '')
        query = str(params.get('q') or '').strip().casefold()
        if len(query) > 200 or len(publisher) > 100:
            raise ValueError('筛选内容过长')
        admin = actor.get('is_admin') is True
        own_star = identity(self.settings.institution_user_id, actor)[0]
        source = _query_report(self.reports.store, dict(period, group_by='promotion'), None if admin else own_star, facts=True)
        root = self.settings.data_dir

        def in_scope(row):
            return (row.get('institution_user_id') == self.settings.institution_user_id
                    and row.get('project_type') == 2 and row.get('status') == 'succeeded'
                    and (admin or (row.get('tenant_key') == actor['tenant_key'] and row.get('user_id') == actor['user_id'])))

        links = [r for r in read_records(root / 'short-links.sqlite3', 'links') if in_scope(r) and publication(r)]
        promotions = [r for r in read_records(root / 'promotions.sqlite3', 'promotions') if in_scope(r)]
        names = {r['star_user_id']: r.get('star_name') or r.get('user_id') or '发布人待归属' for r in promotions}
        story_names = {r['story_id']: r.get('story_name') or r['story_id'] for r in source['list'] if r.get('story_id')}
        missing = sorted({r['story_id'] for r in links} - set(story_names))
        for offset in range(0, len(missing), 500):
            rows = self.reports.store.reader._read("SELECT JSON_OBJECT('story_id',story_id,'story_name',story_name) FROM ads_ai.cdreader_koc_drama WHERE %s AND story_id IN (%s);" % (
                self.reports.store.scope(), ','.join(_literal(v) for v in missing[offset:offset+500])))
            story_names.update({r['story_id']: r['story_name'] for r in rows})
        by_code = {r['short_code']: r for r in links}
        destinations = {}
        for r in links:
            key = (r.get('koc_code'), r.get('story_id'), r.get('star_user_id'))
            destinations.setdefault(key, set()).add((r.get('platform') or 'unknown', str(r.get('account_id') or '')))

        def labels(story, star, code, channel, account, day):
            account_label = '多账号共用' if account == 'shared' else account or '账号待归属'
            return {'platform': channel, 'platform_label': PLATFORMS.get(channel, channel),
                    'publisher': star or 'unknown', 'publisher_label': names.get(star, '发布人待归属'),
                    'story': story or 'unknown', 'story_label': story_names.get(story, story or '剧目待归属'),
                    'promotion': code or 'unknown', 'promotion_label': code or '口令待归属',
                    'account': channel + ':' + (account or 'unknown'),
                    'account_label': PLATFORMS.get(channel, channel) + ' · ' + account_label,
                    'day': day or 'unknown', 'day_label': day or '来源日期未明确'}

        facts = []
        with self.clicks.connect() as conn:
            conn.execute('BEGIN')
            coverage = conn.execute("SELECT value FROM metadata WHERE key='coverage_from'").fetchone()[0]
            for day, code, clicks, automated in conn.execute('SELECT day,short_code,SUM(clicks),SUM(automated) FROM daily WHERE day BETWEEN ? AND ? GROUP BY day,short_code', (str(start), str(end))):
                r = by_code.get(code)
                if not r:
                    continue
                fact = labels(r['story_id'], r['star_user_id'], r['koc_code'], r.get('platform') or 'unknown', str(r.get('account_id') or ''), day)
                fact.update(clicks=clicks, automated=automated, _links={code} if clicks else set())
                facts.append(fact)
        for row in source['list']:
            choices = destinations.get((row['koc_code'], row['story_id'], row['real_star_user_id']), set())
            platforms = {p for p, _ in choices}
            channel = next(iter(platforms)) if len(platforms) == 1 else 'shared' if platforms else 'unknown'
            account = next(iter(choices))[1] if len(choices) == 1 else 'shared' if choices else ''
            names.setdefault(row['real_star_user_id'], row.get('owner_name') or '发布人待归属')
            fact = labels(row['story_id'], row['real_star_user_id'], row['koc_code'], channel, account, row['date'])
            fact.update({k: row[k] for k in MONEY_METRICS + COUNT_METRICS})
            facts.append(fact)

        publisher_options = sorted({(r['publisher'], r['publisher_label']) for r in facts})
        def matches(row):
            return ((not publisher or row['publisher'] == publisher)
                    and (not query or query in ' '.join(str(row.get(k, '')) for k in ('story', 'story_label', 'promotion', 'account', 'publisher_label')).casefold()))
        facts = [r for r in facts if matches(r)]
        excluded_shared = blank()
        if platform:
            for row in facts:
                if row['platform'] != platform and row['platform'] in ('shared', 'unknown'):
                    add(excluded_shared, row)
            facts = [r for r in facts if r['platform'] == platform]
        summary, grouped, daily = blank(), {}, {}
        for row in facts:
            key = tuple(row[d] for d in dimensions)
            if key not in grouped:
                grouped[key] = dict(blank(), dimensions=[{'key': d, 'value': row[d], 'label': row[d+'_label']} for d in dimensions])
            add(grouped[key], row)
            add(summary, row)
            if row['day'] != 'unknown':
                add(daily.setdefault(row['day'], blank()), row)
        rows = [public(r) for r in grouped.values()]
        rows.sort(key=lambda r: (-Decimal(r['total_revenue_usd']), -r['clicks'], str(r['dimensions'])))
        jobs = self.reports._jobs()
        order_ranges = [{'date_from': j['date_from'], 'date_to': j['date_to'], 'updated_at': j.get('updated_at')}
                        for j in jobs if any(p.get('kind') == 'orders' and p.get('status') == 'succeeded' for p in j.get('parts', []))]
        # Day ledger, not nonzero rows: a successfully synced empty day is known zero.
        ad_days = self.reports.store.reader._read("SELECT JSON_OBJECT('day',ad_time) FROM ads_ai.cdreader_koc_ad_day_snapshot WHERE %s AND ad_time BETWEEN CAST(%s AS DATE) AND CAST(%s AS DATE);" % (
            self.reports.store.scope(), _literal(str(start)), _literal(str(end))))
        ad_dates = {r['day'] for r in ad_days}
        dates = [str(start + timedelta(days=i)) for i in range((end-start).days+1)]
        order_dates = {d for d in dates if any(r['date_from'] <= d <= r['date_to'] for r in order_ranges)}
        trend = [dict(public(daily.get(d, blank())), date=d, orders_synced=d in order_dates, ads_synced=d in ad_dates,
                      clicks_covered=d >= coverage) for d in dates]
        return {'view': 'effects', 'currency': 'USD', **period, 'group_by': dimensions,
                'scope': {'kind': 'institution' if admin else 'owner', 'label': '机构全部' if admin else '本人推广'},
                'list': rows, 'total': len(rows), 'summary': public(summary), 'trend': trend,
                'publishers': [{'value': v, 'label': label} for v, label in publisher_options],
                'excluded_shared': public(excluded_shared), 'click_coverage_from': coverage,
                'last_success_at': next((j.get('finished_at') for j in jobs if j['status'] == 'succeeded'), None),
                'order_data_as_of': max((r['date_to'] for r in order_ranges), default=None),
                'ad_data_as_of': source['ad_data_as_of'], 'complete_ad_days': len(ad_dates),
                'complete_order_days': len(order_dates), 'requested_days': len(dates),
                'queried_at': datetime.now(BEIJING).isoformat(), 'source_timezone': 'unspecified'}
