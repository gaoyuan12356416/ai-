"""Unassigned TikTok site effects remain visible only within their verified tenant."""

import os
from collections import defaultdict


UNASSIGNED = '__tiktok_unassigned__'
UNASSIGNED_LABEL = 'TikTok 渠道待归属'


def keyed(metric):
    from .tiktok import campaign_key
    raw = str(metric.get('campaign_id') or '')
    return dict(metric, campaign_id=campaign_key(raw, metric.get('campaign')),
                tiktok_source_kind='missing' if not raw else 'generic' if raw == '0001' else 'campaign')


def reconcile_insight_cents(rows):
    from .bill_income import allocate_tiktok_cents
    days = defaultdict(list)
    for row in rows:
        days[row['date']].append(row)
    for daily in days.values():
        allocate_tiktok_cents(daily, fields=('revenue', 'refund'))
        for row in daily:
            del row['revenue_usd_1e4'], row['refund_usd_1e4']


def site_tenant(catalog):
    tenants = {row.get('tiktok_site_tenant') or '' for row in catalog if row['platform'] == 'tiktok'}
    return next(iter(tenants)) if len(tenants) == 1 else ''


def stamp_site_tenant(catalog, settings):
    configured = settings.get('SOCIAL_ANALYTICS_TT_SITE_TENANT') or os.environ.get('SOCIAL_ANALYTICS_TT_SITE_TENANT')
    tenants = {row['tenant'] for row in catalog if row.get('tenant') and row.get('publications') and not row.get('excluded')}
    trusted = configured if configured and tenants == {configured} else ''
    return [dict(row, tiktok_site_tenant=trusted) for row in catalog]


def unassigned_row(candidates, tenant, day):
    """Only dimensions shared by confirmed, eligible publications may narrow this bucket."""
    eligible = [row for row in candidates if not row.get('excluded') and
                any(fact['date'] <= day for fact in row.get('publications', ()))]
    result = dict(platform='tiktok', platform_label='TikTok', site_id='2250',
                  tenant=tenant, owner=UNASSIGNED, owner_label=UNASSIGNED_LABEL, date=day)
    for dimension in ('drama', 'channel', 'language', 'link_type'):
        values = {row.get(dimension) for row in eligible}
        if len(values) == 1 and next(iter(values)):
            result[dimension] = eligible[0][dimension]
            result[dimension + '_label'] = eligible[0].get(dimension + '_label') or result[dimension]
        else:
            result[dimension] = ''
            result[dimension + '_label'] = '待归属'
    return result


def unassigned_visible(row, actor):
    return actor.get('_system_report') is True or (
        actor.get('role') == 'admin' and bool(actor.get('user_id')) and
        bool(row.get('tenant')) and actor.get('tenant_key') == row['tenant'])
