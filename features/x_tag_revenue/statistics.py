"""Operating counters and portrait income in one immutable published version."""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal

from features.x_account_stats.service import (
    SHANGHAI, _open_ledger, _money_text, read_ledger_metrics,
)


def build_statistics(data, ledger_path, now):
    yesterday = now.astimezone(SHANGHAI).date() - timedelta(days=1)
    counts, _, _ = read_ledger_metrics(ledger_path, yesterday)
    with closing(_open_ledger(ledger_path)) as db:
        accounts = db.execute('SELECT id,x_user_id FROM x_authorized_account').fetchall()
    result = {}
    for account in accounts:
        account_id, uid = int(account['id']), str(account['x_user_id'])
        profile = data['accounts'].get(uid, {})
        result[str(account_id)] = {
            **dict.fromkeys(('published_posts_total', 'published_posts_yesterday',
                             'reposts_total', 'reposts_yesterday'), 0),
            **counts.get(account_id, {}),
            'revenue_total_usd': _money_text(Decimal(profile.get('revenue_cents', 0)) / 100),
            'revenue_yesterday_usd': _money_text(Decimal(profile.get('revenue_yesterday_cents', 0)) / 100),
            'revenue_status': profile.get('status', 'no_history'),
            'x_user_id': uid,
        }
    meta = data['meta']
    audit = data['audit']
    return {
        'schema_version': 1, 'site_id': '2116', 'timezone': 'Asia/Shanghai',
        'generated_at_utc': meta['generated_at'], 'version': meta['version'],
        'business_date': now.astimezone(SHANGHAI).date().isoformat(),
        'yesterday_date': yesterday.isoformat(), 'revenue_yesterday_date': meta['end'],
        'revenue_timezone': 'UTC', 'start': meta['start'], 'end': meta['end'],
        'source_generated_at': meta['source_generated_at'], 'source_version': meta['source_version'],
        'revenue_source': 'social_analytics_warehouse_confirmed_x',
        'refresh_interval_seconds': 3600, 'accounts': result,
        'attribution': {'method': 'verified-warehouse-exact-campaign-v1',
                        'unassigned_revenue_cents': audit['unassigned_revenue_cents'],
                        'unassigned_rows': audit['unassigned_rows']},
        'unallocated_revenue': {
            'total_usd': _money_text(Decimal(audit['unassigned_revenue_cents']) / 100),
            'yesterday_usd': None,
        },
    }
