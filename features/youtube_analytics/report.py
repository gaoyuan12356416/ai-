"""Frozen attribution dimensions and metric aggregation. Never initialize a writer."""
import csv
import io
import json
import math
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DIMENSIONS = ('date', 'owner', 'drama', 'channel', 'link_type', 'language')
FIELDS = ('clicks', 'views', 'installs', 'conversions', 'revenue_cents', 'refund_cents')
METRICS = ('clicks', 'views', 'installs', 'conversions', 'revenue', 'refunds', 'install_rate', 'pay_rate', 'links')
LABELS = dict(date='统计日期（UTC）', owner='生成人', drama='剧名', channel='频道名',
              link_type='链接类型', language='语言', clicks='落地页点击', views='落地页访问',
              installs='安装数', conversions='付费转化数（充值人数）', revenue='收入（USD）',
              refunds='退款（USD）', install_rate='点击安装率（%）', pay_rate='安装付费率（%）', links='生成链接数')


class ReportError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def parameters(query):
    allowed = set(DIMENSIONS[1:]) | {'start', 'end', 'group_by', 'search', 'sort', 'direction', 'page', 'page_size', 'ranking_metric'}
    if set(query) - allowed:
        raise ReportError('存在不支持的筛选参数')
    def one(key, default):
        values = query.get(key, [default])
        if len(values) != 1:
            raise ReportError('重复的参数：' + key)
        return values[0]
    try:
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        end = date.fromisoformat(one('end', yesterday.isoformat()))
        start = date.fromisoformat(one('start', (end - timedelta(days=6)).isoformat()))
        if start > end or (end-start).days >= 93 or end > datetime.now(timezone.utc).date() or start.year < 2020:
            raise ValueError()
        page, size = int(one('page', '1')), int(one('page_size', '50'))
        if not 1 <= page <= 10000 or size not in (20, 50, 100):
            raise ValueError()
    except (ValueError, TypeError):
        raise ReportError('日期范围须为 1–93 天且不晚于今天，分页参数须有效') from None
    groups = one('group_by', 'owner').split(',')
    if not 1 <= len(groups) <= 3 or len(set(groups)) != len(groups) or set(groups)-set(DIMENSIONS):
        raise ReportError('请选择 1–3 个不同的分组维度')
    filters = {}
    for key in DIMENSIONS[1:]:
        values = query.get(key, [])
        if len(values) > 100 or any(not value or len(value)>200 or any(ord(c)<32 for c in value) for value in values):
            raise ReportError('筛选内容过长或无效')
        filters[key] = set(values)
    if filters['link_type'] - {'auto', 'manual'}:
        raise ReportError('链接类型无效')
    search = one('search', '').strip()
    if len(search)>200 or any(ord(c)<32 for c in search):
        raise ReportError('搜索内容无效')
    sort, direction = one('sort', 'clicks'), one('direction', 'desc')
    ranking = one('ranking_metric', 'clicks')
    if sort not in METRICS + tuple(groups) or direction not in ('asc', 'desc') or ranking not in METRICS:
        raise ReportError('排序参数无效')
    return dict(start=start.isoformat(), end=end.isoformat(), groups=groups, filters=filters,
                search=search.casefold(), sort=sort, direction=direction, page=page, page_size=size, ranking_metric=ranking)


def attribution_key(url):
    parsed = urlsplit(url or '')
    if parsed.scheme != 'https' or parsed.hostname != 'www.dramawavew2a.com' or parsed.path != '/ads/101/2284/view':
        return None
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30)
    except ValueError:
        return None
    if any(len(query.get(k, [])) != 1 or not query[k][0] for k in ('af_c_id', 'c')):
        return None
    return query['af_c_id'][0], query['c'][0]


def read_catalog(path):
    """Read only non-secret columns in one consistent SQLite snapshot."""
    deadline = time.monotonic()+10
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=3)) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        c.set_progress_handler(lambda: int(time.monotonic()>deadline), 10000)
        c.execute('BEGIN')
        def rows(sql):
            result = [dict(r) for r in c.execute(sql+' LIMIT 100001')]
            if len(result)>100000:
                raise ReportError('归因记录超过在线报表容量，请联系管理员', 503)
            return result
        users = rows('SELECT user_id,tenant_key,name FROM drama_admin_user')
        links = rows("SELECT id,job_id,material_kind,content_id,long_url,created_at_utc,published_at_utc FROM drama_material_short_link WHERE publish_state='published'")
        automatic = rows('SELECT link_id,context FROM youtube_link_attribution')
        manual = rows('SELECT link_id,tenant,owner,context_json FROM youtube_manual_short_link')
        publications = rows('SELECT job_id,source_kind,operator_user_id,operator_name,channel_id,preparation_id,content_id FROM drama_youtube_publish')
        preparations = rows("SELECT id,tenant,owner,json_extract(body,'$.material.macro_name') AS drama_name,"
                            "json_extract(body,'$.material.language') AS language,"
                            "json_extract(body,'$.channel.name') AS channel_name FROM youtube_auto_preparation")
        jobs = rows('SELECT job_id,drama_name,language FROM drama_material_job')
        c.rollback()
    names = {(r['tenant_key'], r['user_id']): r['name'] for r in users}
    tenants = defaultdict(set)
    for row in users:
        if row['tenant_key']:
            tenants[row['user_id']].add(row['tenant_key'])
    frozen = defaultdict(list)
    for row in automatic:
        frozen[row['link_id']].append(dict(json.loads(row['context']), link_type='auto'))
    for row in manual:
        frozen[row['link_id']].append(dict(json.loads(row['context_json']), tenant=row['tenant'], owner=row['owner'], link_type='manual'))
    prep = {r['id']: r for r in preparations}
    job = {r['job_id']: r for r in jobs}
    published = defaultdict(list)
    for row in publications:
        published[(row['job_id'], row['source_kind'])].append(row)
    catalog = []
    for link in links:
        key = attribution_key(link['long_url'])
        if not key:
            continue
        contexts = frozen.get(link['id'], [])
        if not contexts:
            for publication in published.get((link['job_id'], link['material_kind']), []):
                owner = publication['operator_user_id']
                p = prep.get(publication['preparation_id'])
                tenant = p['tenant'] if p and p['owner']==owner else next(iter(tenants[owner])) if len(tenants[owner])==1 else ''
                snapshot = p or {}
                original = job.get(publication['job_id'], {})
                contexts.append(dict(tenant=tenant, owner=owner, owner_name=publication['operator_name'],
                    excluded=publication['operator_name']=='internal-deployment-canary',
                    channel_id=publication['channel_id'], channel_name=snapshot.get('channel_name') or publication['channel_id'],
                    content_id=link['content_id'], drama_name=snapshot.get('drama_name') or original.get('drama_name') or link['content_id'],
                    language=snapshot.get('language') or original.get('language') or '未记录', link_type='auto'))
        for context in contexts:
            tenant, owner = str(context.get('tenant') or ''), str(context.get('owner') or '')
            ledger_rows = published.get((link['job_id'], link['material_kind']), [])
            canary = context.get('excluded', False) or bool(ledger_rows) and all(
                r['operator_name']=='internal-deployment-canary' for r in ledger_rows)
            catalog.append(dict(key=key, link_id=link['id'], tenant=tenant, owner=owner,
                owner_label=names.get((tenant, owner)) or context.get('owner_name') or owner or '待归属',
                drama=str(context.get('content_id') or link['content_id']), drama_label=context.get('drama_name') or link['content_id'],
                channel=str(context.get('channel_id') or ''), channel_label=context.get('channel_name') or context.get('channel_id') or '未记录',
                language=context.get('language') or '未记录', language_label=context.get('language') or '未记录',
                link_type=context['link_type'], link_type_label='手动短链' if context['link_type']=='manual' else '自动发布',
                date=str(link['created_at_utc'])[:10], excluded=canary))
    return catalog


def visible(row, actor):
    return (bool(actor.get('tenant_key')) and bool(actor.get('user_id')) and row['tenant']==actor['tenant_key']
            and (actor.get('role')=='admin' or row['owner']==actor['user_id']))


def unique_catalog(catalog):
    by_key = defaultdict(list)
    for row in catalog:
        by_key[row['key']].append(row)
    unique, conflicts = {}, {}
    for key, rows in by_key.items():
        identities = {tuple(r.get(k) for k in ('tenant', 'owner', 'drama', 'channel', 'language', 'link_type', 'excluded')) for r in rows}
        if len(identities)==1 and rows[0]['tenant'] and rows[0]['owner'] and not rows[0]['excluded']:
            unique[key] = rows[0]
        else:
            conflicts[key] = rows
    return unique, conflicts


def options(catalog, actor):
    unique, _ = unique_catalog(catalog)
    values = {d: {} for d in DIMENSIONS[1:]}
    for row in unique.values():
        if visible(row, actor):
            for d in values:
                values[d][row[d]] = row.get(d+'_label', row[d])
    return dict(options={d: [dict(value=k, label=v) for k,v in sorted(items.items(), key=lambda x:(x[1],x[0]))]
                        for d,items in values.items()}, scope='tenant' if actor.get('role')=='admin' else 'own')


def empty():
    return dict.fromkeys(FIELDS + ('links',), 0)


def add(target, source):
    for field in FIELDS + ('links',):
        value = Decimal(str(source.get(field) or 0))
        if not value.is_finite() or value != value.to_integral_value() or (field not in ('revenue_cents', 'refund_cents') and value<0):
            raise ReportError('源数据指标异常，暂不能统计', 503)
        target[field] += int(value)


def dto(value, available=True):
    result = dict(value)
    result['revenue'] = round(result.pop('revenue_cents')/100, 2)
    result['refunds'] = round(result.pop('refund_cents')/100, 2)
    result['install_rate'] = round(100*result['installs']/result['clicks'], 2) if result['clicks'] else None
    result['pay_rate'] = round(100*result['conversions']/result['installs'], 2) if result['installs'] else None
    if not available:
        result.update({k: None for k in METRICS if k != 'links'})
    return result


def summarize(catalog, metrics, actor, params, fetched_at):
    unique, conflicts = unique_catalog(catalog)
    def selected(row):
        return visible(row, actor) and all(not values or row[k] in values for k,values in params['filters'].items()) and (
            not params['search'] or params['search'] in (row['drama_label']+' '+row['drama']).casefold())
    selected_rows = {key:row for key,row in unique.items() if selected(row)}
    start, end = date.fromisoformat(params['start']), date.fromisoformat(params['end'])
    days = [(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    totals, groups, trend = empty(), {}, {day:empty() for day in days}
    def emit(dim, value):
        key = tuple(dim[k] for k in params['groups'])
        if key not in groups:
            groups[key] = dict(empty(), **{k: dim[k] for d in params['groups'] for k in (d, d+'_label') if k in dim})
        add(groups[key], value); add(totals, value); add(trend[dim['date']], value)
    seen, populated, source_updated = set(), set(), ''
    unmatched_keys, excluded_keys, unmatched = set(), set(), empty()
    for metric in metrics:
        day = metric['date']
        if day not in trend:
            raise ReportError('源数据日期超出查询范围', 503)
        key = (str(metric.get('campaign_id') or ''), str(metric.get('campaign') or ''))
        if (day,key) in seen:
            raise ReportError('源数据重复，暂不能统计', 503)
        seen.add((day,key)); populated.add(day)
        # Validate every row before trusting source completeness.
        add(empty(), metric)
        source_updated = max(source_updated, str(metric.get('updated_at') or ''))
        if key in selected_rows:
            emit(dict(selected_rows[key], date=day), metric)
        elif key in conflicts and any(selected(r) for r in conflicts[key]):
            # Counts and money are only visible to a same-tenant admin when all
            # candidates belong to that tenant; never leak another owner's data.
            if actor.get('role')=='admin' and all(r['tenant']==actor['tenant_key'] for r in conflicts[key]):
                if all(r['excluded'] for r in conflicts[key]):
                    excluded_keys.add(key)
                else:
                    unmatched_keys.add(key); add(unmatched, metric)
    counted_links = set()
    for row in catalog:
        if row['key'] in selected_rows and row['link_id'] not in counted_links and row['date'] in trend:
            counted_links.add(row['link_id']); emit(row, dict(links=1))
    # Include a zero result for selectable historical links without an event;
    # date grouping needs no artificial historical dates.
    if 'date' not in params['groups']:
        for row in selected_rows.values():
            emit(dict(row,date=days[0]), {})
    available = bool(populated)
    rows = [dto(r, r['date'] in populated if 'date' in params['groups'] else available) for r in groups.values()]
    def sort_key(row, field):
        value = row.get(field+'_label', row.get(field))
        return value if value is not None else -math.inf
    # Stable tie order makes repeated pagination deterministic.
    rows.sort(key=lambda r: tuple(str(r.get(k,'')) for k in params['groups']))
    rows.sort(key=lambda r: sort_key(r,params['sort']), reverse=params['direction']=='desc')
    ranking = sorted(rows, key=lambda r: sort_key(r,params['ranking_metric']), reverse=True)[:10]
    ranking = [dict(r,label=' / '.join(str(r.get(k+'_label',r[k])) for k in params['groups'])) for r in ranking]
    missing = [d for d in days if d not in populated]
    warnings = ['转化数采用源表充值人数；跨日期累加每日人数，未做跨日用户去重。']
    if missing:
        warnings.append('部分日期源表暂无记录，标为未同步；合计仅含已有日期，不能将缺失日期判为零。')
    if params['end']==datetime.now(timezone.utc).date().isoformat():
        warnings.append('今天的数据尚未完整，后续回传会更新。')
    if unmatched_keys:
        warnings.append('存在无法唯一归属的历史链接，已从个人和维度合计排除并单列。')
    total_rows = len(rows); pages = max(1, math.ceil(total_rows/params['page_size']))
    page = min(params['page'], pages); offset=(page-1)*params['page_size']
    return dict(totals=dto(totals,available), rows=rows[offset:offset+params['page_size']], all_rows=rows,
        trend=[dict(dto(trend[d],d in populated),date=d) for d in days], ranking=ranking,
        pagination=dict(page=page,page_size=params['page_size'],total=total_rows,pages=pages), group_by=params['groups'],
        quality=dict(missing_dates=missing, unmatched_campaigns=len(unmatched_keys),unmatched_totals=dto(unmatched,available),excluded_campaigns=len(excluded_keys)),
        meta=dict(start=params['start'],end=params['end'],timezone='UTC',currency='USD',fetched_at=fetched_at,
                  source_updated_at=source_updated,scope='tenant' if actor.get('role')=='admin' else 'own',
                  conversion_label=LABELS['conversions']), warnings=warnings)


def csv_export(report):
    buffer = io.StringIO(newline=''); writer=csv.writer(buffer)
    fields=[]
    for d in report['group_by']:
        if d != 'date': fields.append((d,d+' ID'))
        fields.append((d if d=='date' else d+'_label',LABELS[d]))
    fields += [(m,LABELS[m]) for m in METRICS]
    fields += [('period','统计范围（UTC）'),('data_status','数据完整性'),('conversion_note','转化口径')]
    def safe(value):
        if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@','\t','\r','\n')):
            return "'"+value
        return '未同步' if value is None else value
    writer.writerow([label for _,label in fields])
    for row in report['all_rows']:
        row = dict(row,period=report['meta']['start']+' 至 '+report['meta']['end'],
                   data_status='缺少源数据日期：'+','.join(report['quality']['missing_dates']) if report['quality']['missing_dates'] else '已有源数据',
                   conversion_note='源表充值人数，每日人数累加，未跨日去重')
        writer.writerow([safe(row.get(key)) for key,_ in fields])
    return ('\ufeff'+buffer.getvalue()).encode('utf-8')
