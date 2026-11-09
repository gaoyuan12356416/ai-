"""Explicit DramaWave human-image cohort, separate from the market remainder."""
import datetime as dt
import json
from collections import Counter

from analysis import metrics
from analysis_v3 import same_totals
from collector import literal


def classify_inventory(rows, mapped, unresolved):
    ai_ids = {str(key) for key in mapped}
    ai_signs = {row['content_sign'] for row in mapped.values() if row.get('content_sign')}
    unresolved_ids = {str(row.get('id') or '') for row in unresolved}
    members, excluded, reasons = {}, {}, Counter()
    for row in rows:
        key = str(row['id'])
        remark = str(row.get('remark') or '').strip()
        try:
            meta = json.loads(remark)
        except (ValueError, TypeError):
            meta = {}
        ai_origin = isinstance(meta, dict) and meta.get('origin') == 'ai-cover'
        if key in ai_ids:
            reason = 'verified_ai'
        elif row.get('content_sign') and row['content_sign'] in ai_signs:
            reason = 'ai_signature_unverified'
        elif key in unresolved_ids:
            reason = 'unresolved_lineage'
        elif ai_origin or remark.lower() == 'ai drama' or row.get('task_id') == -2:
            reason = 'ai_import_unverified'
        elif row.get('task_id') is None or int(row['task_id']) < -1:
            reason = 'unknown_origin'
        else:
            # task_id > 0: material task; 0: manual upload; -1: Excel import.
            # This is catalog provenance, not a claim about external design tools.
            members[key] = str(row['created_at'])[:10]
            continue
        excluded[key] = reason
        reasons[reason] += 1
    if len(members) + len(excluded) != len(rows):
        raise ValueError('duplicate_human_inventory_id')
    return dict(members=members, excluded=excluded, excluded_counts=dict(reasons),
                catalog_count=len(rows), rule_version='dramawave-human-v1')


def collect_inventory(catalog, mapped, unresolved, date):
    until = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
    rows = catalog.query("SELECT JSON_OBJECT('id',id,'created_at',created_at,'task_id',task_id,"
                         "'remark',remark,'content_sign',content_sign) FROM ads_custom_source "
                         "WHERE product=" + literal('Dramawave') +
                         " AND type=1 AND data_source IN (0,6) AND created_at<" + literal(until))
    return classify_inventory(rows, mapped, unresolved)


def attach_comparison(report, inventory, mapped, facts):
    """Use exact formal IDs and the same daily MVCC reads as AI performance."""
    apps = {name for name, root in report['app_scope']['app_to_product'].items() if str(root) == '1479'}
    days = [row['dt'] for row in report['views']['1479']['trend']]
    coverage = {row['dt'] for row in facts if row['kind'] == 'human_coverage' and row.get('ok') == 1}
    if coverage != set(days):
        raise ValueError('human_comparison_coverage_incomplete')
    candidates = [row for row in facts if row['kind'] == 'dramawave_image']
    if any(row['app_id'] not in apps for row in candidates):
        raise ValueError('human_delivery_scope_mismatch')
    manual, verified, unknown = [], [], []
    for row in candidates:
        key = str(row['id'])
        if key in inventory['members']:
            manual.append(row)
        elif key in mapped or inventory['excluded'].get(key) == 'verified_ai':
            verified.append(row)
        else:
            unknown.append(row)
    checks = same_totals(manual + verified + unknown, candidates, 'human_partition')
    ai = [row for row in facts if row['kind'] == 'ai' and row['app_id'] in apps]
    if {str(row['id']) for row in manual} & {str(row['id']) for row in ai}:
        raise ValueError('human_ai_overlap')
    cell = lambda row: (row['dt'], row['app_id'], str(row['platform']))
    shared = {cell(row) for row in ai if row['spend'] > 0} & {cell(row) for row in manual if row['spend'] > 0}
    modes = {}
    for mode in ('all', 'matched'):
        select = lambda rows: [row for row in rows if mode == 'all' or cell(row) in shared]
        selected_ai, selected_manual = select(ai), select(manual)
        summary = lambda date: dict(ai=metrics([r for r in selected_ai if not date or r['dt'] == date]),
                                    human=metrics([r for r in selected_manual if not date or r['dt'] == date]))
        trend = [dict(dt=date, **summary(date)) for date in days]
        modes[mode] = dict(day=summary(report['report_date']), week=summary(None), trend=trend)
        for cohort, rows in [('ai', selected_ai), ('human', selected_manual)]:
            checks += same_totals([day[cohort] for day in trend], rows, 'human_trend_' + mode + '_' + cohort)
    report['human_comparison'] = dict(
        product_id='1479', product_name='DramaWave', modes=modes,
        inventory=dict(total=len(inventory['members']),
                       new_day=sum(date == report['report_date'] for date in inventory['members'].values()),
                       new_week=sum(date in days for date in inventory['members'].values()),
                       excluded_counts=inventory['excluded_counts']),
        unknown=dict(day=metrics([row for row in unknown if row['dt'] == report['report_date']]), week=metrics(unknown)),
        rule_version=inventory['rule_version'],
        note='人工图片按 Dramawave 素材库的素材任务、手动上传或 Excel 导入来源归类；排除已验证 AI ID/图片签名、AI 封面标记、AI 接口导入及来源待核对记录。此分类依据系统来源，不推断人工制作时是否使用外部 AI 工具。',
        scope_note='两类效果均限定实际投放到 DramaWave 主 App 及 W2A，按相同 dt、USD、AF D0 口径汇总。共同投放范围仅保留两类在同一天都有消耗的同应用、同渠道；表现差异不代表因果增量。')
    report['validation']['checks'] += checks + 2
    report['validation']['human_comparison'] = 'passed'

