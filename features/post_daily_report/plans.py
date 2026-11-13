"""Frozen targets that have no execution task. Read-only, account-level dedupe."""
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import sqlite3
import hashlib
import random
import time
import copy
import threading

from .common import BEIJING, Window, parse_time, read_db
from .accounting import object_value, read_rows, tables
from .target_plans import load_target_plans, POLICY


def _slot(day, value):
    try:
        return datetime.fromisoformat(str(day) + 'T' + str(value)).replace(tzinfo=BEIJING).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _accounts(raw):
    return sorted({str(x) for x in object_value(raw, []) or []})


_CACHE = {}
_LIVE = {}
_LIVE_LOCK = threading.Lock()


def live_plan_gaps(paths, channels):
    """Keep expensive multi-ledger scans out of the coordinator HTTP handlers."""
    key=(tuple(sorted((k,str(v)) for k,v in paths.items() if not k.startswith('_'))),tuple(channels))
    with _LIVE_LOCK:
        entry=_LIVE.setdefault(key, {'loading':False,'updated':0,'result':None,'error':False})
        if not entry['loading'] and time.monotonic()-entry['updated']>30:
            entry['loading']=True
            def refresh():
                at=datetime.now(timezone.utc)
                try:
                    result=collect_plan_gaps(paths,at,channels)
                    result['as_of']=at.isoformat()
                except Exception:
                    result=None
                with _LIVE_LOCK:
                    if result is not None:entry['result']=result
                    entry['error']=result is None
                    entry['loading']=False;entry['updated']=time.monotonic()
            threading.Thread(target=refresh,name='monitor-plan-read',daemon=True).start()
        result=copy.deepcopy(entry['result'])
        if result and entry['error']:
            result['warnings'].append('最新计划扫描失败，暂保留上次成功核对的计划快照；见最近核对时刻')
    return result or {'available':False,'items':[],'warnings':['正在后台只读核对未入队计划；首次完成前数量显示未知，请稍后刷新']}


def collect_plan_gaps(paths, cutoff, channels=('tt', 'fb', 'x', 'youtube')):
    key = (tuple(sorted((k, str(v)) for k,v in paths.items() if not k.startswith('_'))), tuple(channels), cutoff.isoformat() if abs((datetime.now(timezone.utc)-cutoff).total_seconds()) > 60 else cutoff.replace(second=0,microsecond=0).isoformat())
    cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 30:
        return copy.deepcopy(cached[1])
    result = _collect_plan_gaps(paths, cutoff, channels)
    if len(_CACHE) >= 8:
        _CACHE.clear()
    _CACHE[key] = (time.monotonic(), copy.deepcopy(result))
    return result


def _days(begin, cutoff, channel, unavailable):
    day, last = begin.astimezone(BEIJING).date(), cutoff.astimezone(BEIJING).date()
    if (last-day).days > 370:
        unavailable(channel, '较早历史超出逐日核对范围，已核实数量单列')
        day = last - timedelta(days=370)
    while day <= last:
        yield day.isoformat()
        day += timedelta(days=1)


def _auto_slots(db, prefix, channel, runs, versions, cutoff, add, unavailable):
    existing = {(r['template_id'],r['template_version'],r['shanghai_date'],r.get('publish_time')) for r in runs if r.get('trigger_type') != 'manual'}
    activation, target_plans = load_target_plans(db, 'x:auto' if channel == 'x' else prefix)
    target_runs = set()
    target_days = set()
    if channel in {'tt', 'x'}:
        for run in runs:
            if run.get('trigger_type') == 'manual':
                continue
            account = str(run.get('target_account_id') or '')
            if account or run.get('schedule_policy') == POLICY:
                target_days.add((run['template_id'], run['shanghai_date']))
            if account:
                due = parse_time(run.get('scheduled_at_utc')) or _slot(run.get('shanghai_date'), run.get('publish_time'))
                target_runs.add((run['template_id'], run['shanghai_date'], account, due))
        for (template_id, day), plan in target_plans.items():
            midnight = _slot(day, '00:00')
            if midnight is None:
                unavailable(channel, '账号随机日计划日期无效'); continue
            if midnight >= cutoff:
                continue
            if plan['error']:
                unavailable(channel, plan['error'])
            for slot in plan['slots']:
                identity = (template_id, day, slot['account_id'], slot['due'])
                if identity not in target_runs:
                    add(channel, f'target-slot:{template_id}:{day}:{slot["ordinal"]}', slot['account_id'], slot['due'])
    history_by_template = defaultdict(list)
    if prefix + '_event' in tables(db):
        events = db.execute('SELECT event_type,details_json,created_at FROM ' + prefix + "_event WHERE event_type IN ('template_enabled','template_disabled','template_updated')")
        for event in events:
            detail = object_value(event['details_json'], {}) or {}
            at = parse_time(event['created_at'])
            if isinstance(detail, dict) and at:
                history_by_template[str(detail.get('template_id'))].append((at, event['event_type'], detail))
        for history in history_by_template.values():
            history.sort(key=lambda x: x[0])
    random_plans={(p['template_id'],p['template_version'],p['shanghai_date']):object_value(p.get('publish_times_json'),[]) for p in read_rows(db,prefix+'_random_plan')}
    frozen={(v['template_id'],v['version']):v for v in read_rows(db,prefix+'_template_version')}
    for template in read_rows(db,prefix+'_template'):
        history = history_by_template.get(str(template['id']), [])
        if not history:
            unavailable(channel, f"模板 {template['id']} 缺少历史启停，未建运行的计划数量未知");continue
        for index,(begin,kind,detail) in enumerate(history):
            if kind!='template_enabled' or begin>=cutoff:continue
            finish=min(history[index+1][0],cutoff) if index+1<len(history) else cutoff
            version=detail.get('version'); config=versions.get((template['id'],version))
            if config is None:
                unavailable(channel,'启用记录缺少冻结版本');continue
            schedule=config.get('schedule',{})
            for day in _days(begin,finish,channel,unavailable):
                if _slot(day,'00:00')>=finish:break
                if channel in {'tt', 'x'}:
                    # A saved day survives later template edits and owns its
                    # target times; the old whole-template draw is not additive.
                    if (template['id'], day) in target_plans:
                        continue
                    if ((day >= activation and schedule.get('mode') == 'random')
                            or (template['id'], day) in target_days):
                        # A random template enabled after midnight starts on the
                        # next day. Missing evidence at midnight stays unknown.
                        if begin <= _slot(day, '00:00'):
                            unavailable(channel, '按账号随机日计划缺失，未使用旧随机算法推算')
                        continue
                times=schedule.get('times',[]) if schedule.get('mode')=='fixed' else random_plans.get((template['id'],version,day))
                if times is None and channel=='tt':
                    digest=frozen.get((template['id'],version),{}).get('config_sha256')
                    if digest:
                        seed=f"{template['id']}|{version}|{digest}|{day}"
                        rng=random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest()[:16],'big'))
                        times=['%02d:%02d'%divmod(m,60) for m in sorted(rng.sample(range(1440),min(max(int(schedule.get('daily_count') or 0),0),1440)))]
                if times is None:
                    unavailable(channel,'启用区间内随机日计划缺失');continue
                for value in times:
                    slot=(template['id'],version,day,value);due=_slot(day,value)
                    if slot in existing or not due or not begin<=due<finish:continue
                    for account in _accounts(config.get('account_ids')):
                        add(channel,'auto-slot:'+':'.join(map(str,slot)),account,due)


def _legacy_tt_slots(db, occupied, cutoff, add, unavailable):
    audits=read_rows(db,'tt_post_daily_schedule_audit')
    plans={(str(p['account_id']),p['shanghai_date']):p for p in read_rows(db,'tt_post_random_daily_plan')}
    for schedule in read_rows(db,'tt_post_daily_schedule'):
        account=str(schedule['account_id']);history=[]
        for a in audits:
            config=object_value(a.get('snapshot_json'),{}) or {};at=parse_time(a.get('created_at'))
            if str(config.get('account_id',a.get('account_id')))==account and at and at<cutoff:
                history.append((at,config))
        history.sort(key=lambda x:x[0])
        if not history:
            if schedule.get('enabled') or any(k[0]==account for k in plans):
                unavailable('tt','人工素材排期缺少历史启停，已存在运行仍逐条统计')
            continue
        for index,(begin,config) in enumerate(history):
            if not config.get('enabled'):continue
            finish=history[index+1][0] if index+1<len(history) else cutoff
            for day in _days(begin,finish,'tt',unavailable):
                if _slot(day,'00:00')>=finish:break
                if config.get('schedule_mode')=='random':
                    plan=plans.get((account,day))
                    if not plan or plan.get('config_version')!=config.get('version'):
                        unavailable('tt','人工素材启用区间内随机日计划缺失');continue
                    times=object_value(plan.get('publish_times_json'),[]) or []
                else:times=object_value(config.get('publish_times_json'),[]) or []
                for value in times:
                    due=_slot(day,value)
                    if due and begin<=due<finish and (account,due) not in occupied:
                        add('tt',f'material-slot:{day}:{value}',account,due)


def _collect_plan_gaps(paths, cutoff, channels):
    out = {'available': True, 'items': [], 'warnings': [], 'channel_available': {c: True for c in channels}}
    seen = set()

    def add(channel, key, account, due, code='', closed=False):
        identity = (channel, key, str(account))
        if not due or due >= cutoff or identity in seen:
            return
        seen.add(identity)
        out['items'].append({'channel': channel, 'plan_key': key, 'account_id': str(account),
                             'due_at': due.isoformat(), 'code': 'plan_cancelled' if closed else code or 'plan_not_enqueued',
                             'closed': bool(closed)})

    def unavailable(channel, detail):
        out['available'] = out['channel_available'][channel] = False
        message = channel.upper() + ' 未入队计划存在缺失证据：' + detail
        if message not in out['warnings']:
            out['warnings'].append(message)

    if 'x' in channels:
        try:
            from .x import _root_ids, _pool_plans, _row
            with read_db(paths['x']) as db:
                required = {'x_post_schedule_run', 'x_post_queue'}
                if not required <= tables(db):
                    raise ValueError('缺少排期或队列表')
                root = _root_ids(db)
                all_runs = read_rows(db, 'x_post_schedule_run')
                runs = {r['id']: r for r in all_runs if root(r['id']) == r['id']}
                queues = read_rows(db, 'x_post_queue')
                queued = {(root(q['schedule_run_id']), str(q['account_id'])) for q in queues if q.get('schedule_run_id')}
                cancelled = {(r['run_id'], str(r['account_id'])) for r in read_rows(db, 'x_post_unqueued_cancellation')
                             if parse_time(r.get('created_at')) and parse_time(r['created_at']) < cutoff}
                dates = {r['run_date'] for r in runs.values() if r.get('run_date')}
                dates.update(r['run_date'] for r in read_rows(db, 'x_post_schedule_random_plan'))
                audits = read_rows(db, 'x_post_schedule_config_audit')
                stamps = [parse_time(r.get('created_at')) for r in audits]
                stamps = [s for s in stamps if s]
                if stamps:
                    day = min(stamps).astimezone(BEIJING).date()
                    last = cutoff.astimezone(BEIJING).date()
                    if (last-day).days > 370:
                        unavailable('x', '固定排期历史超过有界读取范围；已知缺口保留')
                        day = last - timedelta(days=370)
                    while day <= last:
                        dates.add(day.isoformat()); day += timedelta(days=1)
                by_slot = defaultdict(list)
                for run in runs.values():
                    by_slot[(run['source_type'], run['run_date'], run.get('publish_time'))].append(run)
                for day in sorted(dates):
                    if day > cutoff.astimezone(BEIJING).date().isoformat():
                        continue
                    window = Window.for_date(day)
                    groups = {s + '_pool': _row(s + '_pool') for s in ('material','drama')}
                    plans, quotas = _pool_plans(db, window, runs, groups)
                    for (source, when), accounts in plans.items():
                        due = _slot(day, when)
                        for account in accounts:
                            run = next((r for r in by_slot.get((source, day, when), []) if account in _accounts(r.get('account_ids_json'))), {})
                            if (run.get('id'), str(account)) not in queued:
                                add('x', 'schedule:' + str(run.get('id') or f'{source}:{day}:{when}'), account, due,
                                    run.get('error_code'), (run.get('id'), str(account)) in cancelled or run.get('status') in {'cancelled', 'canceled'} or run.get('error_code') == 'x_ordinary_lane_retired')
                    # A missing random time is a proven whole-day quota only after day end.
                    if window.end <= cutoff:
                        for source, number in quotas.items():
                            for position in range(number):
                                add('x', f'random-quota:{source}:{day}:{position}', '', window.end - timedelta(microseconds=1))
                    # Missing historical audit before the first audited date is not
                    # evidence of extra plans. Existing frozen runs remain usable.
                    if stamps and window.start >= min(stamps) and any(g['expected'] is None for g in groups.values()):
                        unavailable('x', f'{day} 缺少完整冻结计划，已知缺口单列')
                manual_queued = defaultdict(set)
                for q in queues:
                    if q.get('manual_run_id'):
                        manual_queued[q['manual_run_id']].add(str(q['account_id']))
                for run in read_rows(db, 'x_post_manual_run'):
                    if run.get('trigger_source', 'manual') != 'manual':
                        continue
                    due = parse_time(run.get('scheduled_at') if run.get('publish_mode') == 'scheduled' else run.get('created_at'))
                    for account in _accounts(run.get('account_ids_json')):
                        if account not in manual_queued[run['id']]:
                            add('x', f'manual:{run["id"]}', account, due, run.get('error_code'), run.get('status') in {'cancelled','canceled'})
        except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
            unavailable('x', type(exc).__name__)

    # Template targets use their frozen version, not the template's current scope.
    for channel, path_key, prefix in [('x','x_auto','x_auto'), ('tt','tt_auto','tt_auto')]:
        if channel not in channels:
            continue
        try:
            with read_db(paths[path_key]) as db:
                runs = read_rows(db, prefix + '_run')
                versions = {(r['template_id'],r['version']): object_value(r['config_json'], {}) for r in read_rows(db,prefix+'_template_version')}
                tasks = read_rows(db,prefix+'_task')
                if channel == 'x':
                    from .x import _target_account
                    tasks_by_run = defaultdict(list)
                    for task in tasks:
                        tasks_by_run[task['run_id']].append(task)
                    for run in runs:
                        target = _target_account(run, tasks_by_run[run['id']])
                        if target is not None:
                            run['target_account_id'] = target
                            run['schedule_policy'] = POLICY
                actual = {(r['run_id'],str(r['account_id'])) for r in tasks}
                for run in runs:
                    due = parse_time(run.get('scheduled_at_utc')) or _slot(run.get('shanghai_date'), run.get('publish_time'))
                    is_target_run = run.get('trigger_type') != 'manual'
                    target = str(run.get('target_account_id') or '') if is_target_run else ''
                    if target:
                        accounts = [target]
                    elif is_target_run and run.get('schedule_policy') == POLICY:
                        unavailable(channel, '按账号随机运行缺少目标账号'); continue
                    else:
                        config = versions.get((run['template_id'],run['template_version']))
                        if config is None:
                            unavailable(channel,'模板冻结版本缺失'); continue
                        accounts = _accounts(config.get('account_ids'))
                    for account in accounts:
                        if (run['id'],account) not in actual:
                            add(channel,f'auto:{run["id"]}',account,due,run.get('error_code'),run.get('status') in {'cancelled','canceled'})
                _auto_slots(db, prefix, channel, runs, versions, cutoff, add, unavailable)
        except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
            unavailable(channel,type(exc).__name__)

    if 'fb' in channels:
        try:
            from .fb import _quota, _has_frequency, _superseded_at
            with read_db(paths['fb']) as db:
                runs = read_rows(db,'fb_auto_run'); tasks = read_rows(db,'fb_auto_task')
                pages = read_rows(db,'fb_auto_run_page')
                plans = read_rows(db,'fb_auto_schedule_plan')
                versions = read_rows(db,'fb_auto_template_version')
                version_times={(r['template_id'],r['version']):parse_time(r.get('created_at_utc')) for r in versions}
                version_config={(r['template_id'],r['version']):r['config_json'] for r in versions}
                scope=defaultdict(set); actual=defaultdict(set)
                for r in pages or tasks:scope[r['run_id']].add(str(r['page_id']))
                for t in tasks:actual[t['run_id']].add(str(t['page_id']))
                for run in runs:
                    due=parse_time(run.get('planned_publish_at_utc'))
                    if not due or due>=cutoff:continue
                    allowed=_quota(run,scope[run['id']],plans)
                    if allowed is None or not _has_frequency(run) and len(scope[run['id']])!=int(run.get('total_pages') or 0):
                        unavailable('fb','冻结 Page 范围不完整');continue
                    for page in allowed-actual[run['id']]:add('fb',f'auto:{run["id"]}',page,due)
                snapshots={r['due_slot_id']:r for r in read_rows(db,'fb_auto_due_target_snapshot')}
                run_ids={r['id'] for r in runs}
                due_slots=read_rows(db,'fb_auto_due_slot')
                observed={(r.get('template_id'),r.get('template_version'),parse_time(r.get('planned_publish_at_utc'))) for r in runs+due_slots}
                for plan in plans:
                    for value in object_value(plan.get('times_json'),[]) or []:
                        when=_slot(plan.get('local_date'),value)
                        if when and when<cutoff and (plan.get('template_id'),plan.get('template_version'),when) not in observed and not _superseded_at(plan,when,version_times,cutoff):
                            unavailable('fb','冻结日计划缺少时隙与 Page 快照，数量未知')
                for due in due_slots:
                    when=parse_time(due.get('planned_publish_at_utc'))
                    if due.get('run_id') in run_ids or not when or when>=cutoff:continue
                    if _superseded_at(due,when,version_times,cutoff):continue
                    snap=snapshots.get(due['id'])
                    if snap is None:
                        unavailable('fb','未生成运行的时隙没有 Page 快照');continue
                    frozen={**due,'total_pages':snap['expected_pages'],'config_json':version_config.get((due.get('template_id'),due.get('template_version')))}
                    allowed=_quota(frozen,object_value(snap.get('page_ids_json'),[]) or [],plans)
                    if allowed is None:
                        unavailable('fb','未生成运行的时隙缺少冻结频次');continue
                    for page in allowed:add('fb',f'due:{due["id"]}',page,when,due.get('error_code'))
        except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
            unavailable('fb',type(exc).__name__)

    if 'tt' in channels:
        try:
            with read_db(paths['tt']) as db:
                queues={r['id'] for r in read_rows(db,'tt_post_queue')}
                runs=read_rows(db,'tt_post_schedule_run')
                occupied=set()
                for r in runs:
                    due=parse_time(r.get('scheduled_at_utc'))
                    occupied.add((str(r.get('account_id')),due))
                    if r.get('queue_id') not in queues:
                        add('tt',f'material:{r["id"]}',r.get('account_id'),due,r.get('error_code'),r.get('status') in {'cancelled','canceled','skipped'})
                _legacy_tt_slots(db, occupied, cutoff, add, unavailable)
        except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
            unavailable('tt',type(exc).__name__)
    if 'x' in channels and (paths.get('x_tag') or paths.get('x_tag_auto')):
        profile = _collect_plan_gaps({'x': paths.get('x_tag'), 'x_auto': paths.get('x_tag_auto')}, cutoff, ('x',))
        if not profile['available']:
            out['available'] = out['channel_available']['x'] = False
        out['warnings'].extend('画像：' + w for w in profile['warnings'])
        for item in profile['items']:
            item = dict(item, plan_key='profile:' + item['plan_key'])
            identity = (item['channel'], item['plan_key'], item['account_id'])
            if identity not in seen:
                seen.add(identity)
                out['items'].append(item)
    return out
