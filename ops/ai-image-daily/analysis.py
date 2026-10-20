"""Auditable aggregates; all ratios are recomputed from sums."""
import datetime as dt
from collections import defaultdict
from decimal import Decimal
from collector import FIELDS, TZ


def totals(rows):
    return {key: sum((Decimal(str(row.get(key, 0) or 0)) for row in rows), Decimal(0)) for key in FIELDS}


def metrics(rows):
    value = totals(rows)
    result = {key: float(number) for key, number in value.items()}
    for key, num, den, scale in [('roas','af_revenue_d0','spend',100),('ctr','clicks','impressions',100),('cpi','spend','installs',1)]:
        result[key] = float(value[num] / value[den] * scale) if value[den] else None
    result['spending_materials'] = len({str(r['id']) for r in rows if r.get('id') is not None and Decimal(str(r.get('spend',0))) > 0})
    return result


def difference(all_rows, excluded):
    a, b = totals(all_rows), totals(excluded)
    result = {key: a[key] - b[key] for key in FIELDS}
    if any(value < 0 for value in result.values()):
        raise ValueError('negative_comparison_remainder')
    return metrics([result])


def validate_pool(facts, mapped):
    checks = 0
    # Same-snapshot independent product-pool totals match identified AI subset.
    for day in sorted({r['dt'] for r in facts}):
        ai = [r for r in facts if r['kind']=='ai' and r['dt']==day and r['product']=='Drama-AI素材']
        pool = [r for r in facts if r['kind']=='pool' and r['dt']==day and str(r['id']) in mapped]
        a,b = totals(ai),totals(pool)
        for key in FIELDS:
            if a[key] != b[key]:
                raise ValueError('pool_conservation_failed_' + day + '_' + key)
            checks += 1
        verified = [row for row in facts if row['kind']=='verified_pool' and row['dt']==day]
        if verified or any(item.get('api_version') == 'material_source_v1' for item in mapped.values()):
            if not verified:
                raise ValueError('verified_pool_missing_' + day)
            all_ai = [row for row in facts if row['kind']=='ai' and row['dt']==day]
            a, b = totals(all_ai), totals(verified)
            for key in FIELDS:
                if a[key] != b[key]:
                    raise ValueError('verified_pool_conservation_failed_' + day + '_' + key)
                checks += 1
    return checks


def first_ingestions(mapped):
    """Count source image versions once, on their earliest formal ingestion day.

    Migration-only rows never invent a new original when its old formal mapping
    is unavailable. The formal-ID ingestion and performance counts remain intact.
    """
    versions = defaultdict(list)
    for item in mapped.values():
        versions[item.get('identity') or 'formal:' + str(item['id'])].append(item)
    first = {}
    for identity, rows in versions.items():
        originals = [row for row in rows if row.get('backfill_kind') != 'migration']
        if originals:
            first[identity] = min(originals, key=lambda row: (str(row['created_at']), str(row['id'])))
    return first


def build_report(mapped, unresolved, facts, report_date, generated_at, optimizer_names=None, snapshot_kind='scheduled'):
    optimizer_names = optimizer_names or {}
    checks = validate_pool(facts, mapped)
    day = dt.date.fromisoformat(report_date)
    days = [(day-dt.timedelta(days=i)).isoformat() for i in range(6,-1,-1)]
    ai = [r for r in facts if r['kind']=='ai']
    market = [r for r in facts if r['kind']=='market']
    current = [r for r in ai if r['dt']==report_date]
    previous = [r for r in ai if r['dt']==(day-dt.timedelta(days=1)).isoformat()]
    first = first_ingestions(mapped)
    first_today = [row for row in first.values() if str(row['created_at'])[:10] == report_date]
    by_material = defaultdict(list)
    for row in current:
        if str(row['id']) not in mapped:
            raise ValueError('unmapped_ai_fact')
        by_material[str(row['id'])].append(row)
    materials=[]
    for identity, item in mapped.items():
        # The first report excludes the next day's backfill, even though its original art is older.
        if str(item['created_at'])[:10] > report_date:
            if by_material[identity]:
                raise ValueError('spend_precedes_formal_ingestion_' + identity)
            continue
        public = {key:item.get(key,'') for key in ('id','name','url','source','owner','content_id','language','ratio','created_at','generated_at','is_backfill','is_delete','backfill_kind','legacy_final_ids')}
        representative = first.get(item.get('identity') or 'formal:' + str(item['id']))
        public['first_ingestion_at'] = representative['created_at'] if representative else ''
        public['is_first_ingestion'] = bool(representative and str(representative['id']) == str(item['id']))
        rows = by_material[identity]
        public.update(metrics(rows))
        public['optimizers'] = sorted({optimizer_names.get(str(r['optimizer']),str(r['optimizer'])) for r in rows})
        materials.append(public)
    materials.sort(key=lambda r:(-r['spend'],r['id']))
    new = [r for r in materials if r['created_at'][:10]==report_date]
    production = dict(new_count=len(new),backfill_count=sum(bool(r['is_backfill']) for r in new),
                      migration_count=sum(r['backfill_kind']=='migration' for r in new),
                      new_image_count=len(first_today),
                      dramas=len({r['content_id'] for r in new if r['content_id']}),total_asof=len(materials))
    groups={}
    for name,key in [('sources','source'),('owners','owner'),('ratios','ratio'),('dramas','content_id')]:
        buckets=defaultdict(list)
        for row in materials:
            buckets[row[key] or '未知'].append(row)
        groups[name]=[]
        for label,rows in buckets.items():
            fresh=[r for r in rows if r['created_at'][:10]==report_date]
            groups[name].append(dict(name=label,count=len(rows),new_count=len(fresh),
                                     backfill_count=sum(bool(r['is_backfill']) for r in fresh),
                                     migration_count=sum(r['backfill_kind']=='migration' for r in fresh),
                                     new_image_count=sum(r['is_first_ingestion'] for r in fresh),
                                     **metrics(rows)))
        groups[name].sort(key=lambda r:(-r['spend'],-r['new_count'],r['name']))
        for field in FIELDS:
            if abs(totals(groups[name])[field]-totals(current)[field]) > Decimal('0.000001'):
                raise ValueError('group_conservation_failed_' + name + '_' + field)
            checks += 1
        if sum(r['new_count'] for r in groups[name])!=len(new):
            raise ValueError('production_conservation_failed')
        if sum(r['new_image_count'] for r in groups[name]) != len(first_today):
            raise ValueError('first_ingestion_conservation_failed')
        checks+=1
    optimizers=defaultdict(list)
    for row in current:
        optimizers[str(row['optimizer'])].append(row)
    groups['optimizers']=[dict(name=optimizer_names.get(k,k),count=len({r['id'] for r in rows}),new_count=len({r['id'] for r in rows if mapped[str(r['id'])]['created_at'][:10]==report_date}),**metrics(rows)) for k,rows in optimizers.items()]
    groups['optimizers'].sort(key=lambda r:-r['spend'])
    if totals(groups['optimizers'])!=totals(current):
        raise ValueError('optimizer_conservation_failed')
    checks+=len(FIELDS)
    trend=[]
    for date in days:
        a=[r for r in ai if r['dt']==date]
        m=[r for r in market if r['dt']==date]
        # Only channels/apps with actual positive AI spend are comparable.
        cells={(r['platform'],r['app_id']) for r in a if Decimal(str(r['spend']))>0}
        same=[r for r in m if r['resource_type']==1 and (r['platform'],r['app_id']) in cells]
        excluded=[r for r in a if (r['platform'],r['app_id']) in cells]
        other=difference(same,excluded) if cells else metrics([])
        trend.append(dict(dt=date,ai=metrics(a),market=metrics(m),other_images=other,
                          uploads=sum(r['created_at'][:10]==date for r in mapped.values()),
                          migration_count=sum(r['created_at'][:10]==date and r.get('backfill_kind')=='migration' for r in mapped.values()),
                          new_image_count=sum(str(r['created_at'])[:10]==date for r in first.values())))
    pool_unknown=defaultdict(list)
    for row in facts:
        if row['kind']=='pool' and str(row['id']) not in mapped:
            pool_unknown[str(row['id'])].append(row)
    unresolved=list(unresolved)+[dict(id=k,name='',source='unknown',reason='AI 产品池存在指标但尚未验证后台来源',**metrics(v)) for k,v in pool_unknown.items()]
    def latest(rows):
        return max((r.get('latest_updated') or '' for r in rows),default='')
    return dict(schema_version=2,report_date=report_date,generated_at=generated_at,timezone='Asia/Shanghai',currency='USD',
                snapshot_kind=snapshot_kind,period=[days[0],days[-1]],
                freshness=dict(ai_updated_at=latest(ai),market_updated_at=latest(market)),
                summary=dict(production=production,performance=metrics(current)),
                comparisons=dict(previous=metrics(previous),market=trend[-1]['market'],other_images=trend[-1]['other_images']),
                trend=trend,groups=groups,materials=materials,unresolved=unresolved,
                validation=dict(checks=checks,status='passed',unknown_spending_materials=len(pool_unknown)),
                notes=['按后台日表 dt 选取北京时间前一天；未重新换算广告账号当地自然日。金额沿用后台美元口径。',
                       'AF D0 ROAS = AF D0 收入合计 / 消耗合计；不混加平台收入或 IAA 收入。比例由合计重算，零分母显示 —。',
                       '正式入库数按素材创建时间及正式 ID 统计，含补传；首次入库原图数按来源图片版本去重，不含接口迁移补传，均不是当日生成张数。原图生成时间另列。',
                       '迁移补传与 YouTube 存量补传分别标记。新旧正式 ID 的真实投放指标分别计入后求和，无消耗图片仍计入相应入库数。',
                       '已删除图片保留历史投放指标；负责人表示素材归属，优化师表示实际投放归属。',
                       '同类对照为当日 AI 有消耗的相同应用、相同渠道下其他图片；不把关联差异解读为因果增量。',
                       '每日原始日报保留；近 7 天趋势按本次查询重新计算，归因回补会改变结果。初次补建日报不代表历史 09:00 快照。'])
