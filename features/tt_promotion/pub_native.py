"""Adapters around the installed TT selection and Direct Post implementation."""
import contextlib
import copy
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from .pub_store import connection, iso, load, now


class PublicationError(ValueError):
    def __init__(self, message, status=400, code='tt_promotion_invalid'):
        super().__init__(message)
        self.status, self.code = status, code


def number(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PublicationError(label+'超出范围')
    return value


def old_databases():
    return (os.environ.get('TT_PROMOTION_OLD_AUTO_DB','/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3'),
            os.environ.get('TT_AUTO_POST_LEGACY_DB_PATH','/mnt/data-disk/tt-post-publisher/tt-post.sqlite3'))


class OldHistory:
    def __init__(self, legacy, old_path, app_path, omit=None, extra=()):
        self.legacy, self.old, self.app = legacy, old_path, app_path
        self.omit, self.extra = omit or '', set(extra)

    def seen_material_ids(self, ids):
        result = set(self.legacy.seen_material_ids(ids)) | self.extra.intersection(ids)
        if not ids:
            return result
        with connection(self.old) as old, connection(self.app) as app:
            for offset in range(0,len(ids),400):
                batch=list(ids[offset:offset+400]); marks=','.join('?' for _ in batch)
                result.update(str(row[0]) for row in old.execute(
                    'SELECT material_id FROM tt_auto_material_ledger WHERE material_id IN ('+marks+')',batch))
                result.update(str(row[0]) for row in app.execute('SELECT material_id FROM ttp_slot '
                    'WHERE slot_key<>? AND material_id IN ('+marks+')',[self.omit]+batch))
        return result


class MetricCache:
    """Borrow only native metric queries. The legacy ledger connection is read-only."""
    def __init__(self,path):
        self.path=path

    def _reader(self):
        return connection(self.path)

    def ready_metric_dates(self,*a,**kw):
        from features.tt_auto_posts.core import TTPostAutoStore
        return TTPostAutoStore.ready_metric_dates(self,*a,**kw)

    def iter_ready_metric_rows(self, platform,metric_dates,content_ids=None,*,product=None):
        from features.tt_auto_posts.core import TTPostAutoStore
        ids=list(content_ids) if content_ids is not None else [None]
        for i in range(0,len(ids),400):
            yield from TTPostAutoStore.iter_ready_metric_rows(self,platform=platform,
                metric_dates=metric_dates,content_ids=None if ids==[None] else ids[i:i+400],product=product)


def conflicts(account, at):
    """Read scheduled slots and unresolved writes from both previous owners."""
    old,pool=old_databases()
    results=[]
    acct=str(account)
    with connection(old) as d:
        for row in d.execute("""SELECT id,status,scheduled_at_utc FROM tt_auto_task WHERE account_id=?
            AND (status IN ('publishing','unknown','reconciling') OR
            (status IN ('selecting','preparing') AND julianday(lease_expires_at_utc)>julianday('now')) OR
            (abs((julianday(scheduled_at_utc)-julianday(?))*86400)<60 AND
             status NOT IN ('failed','cancelled','canceled','no_candidate'))) ORDER BY id LIMIT 5""",(acct,at)):
            results.append('旧自动任务 #%s (%s)'%(row[0],row[1]))
        if d.execute("""SELECT 1 FROM social_random_slot s JOIN tt_auto_template t
            ON s.source_key='tt_auto:'||t.id AND t.enabled=1
            WHERE s.target_id=? AND s.local_date=substr(datetime(?, '+8 hours'),1,10)
            AND abs((julianday(s.planned_at)-julianday(?))*86400)<60 LIMIT 1""",(acct,at,at)).fetchone():
            results.append('旧自动模板随机时段')
        local=at_local(at)
        for row in d.execute("""SELECT v.config_json FROM tt_auto_template t JOIN tt_auto_template_version v
                ON t.id=v.template_id AND t.current_version=v.version WHERE t.enabled=1"""):
            cfg=load(row[0]);schedule=cfg.get('schedule',{})
            if acct in map(str,cfg['account_ids']) and local in schedule.get('times',[]):
                results.append('旧自动模板固定时段')
    with connection(pool) as d:
        for row in d.execute("""SELECT id,status FROM tt_post_queue WHERE account_id=?
            AND (status IN ('publishing','reconciling','unknown') OR
            (abs((julianday(scheduled_at_utc)-julianday(?))*86400)<60 AND
            status NOT IN ('failed','canceled','cancelled'))) ORDER BY id LIMIT 5""",(acct,at)):
            results.append('旧素材池任务 #%s (%s)'%(row[0],row[1]))
        for row in d.execute("SELECT id,status FROM tt_post_direct_test WHERE account_id=? AND status IN ('publishing','reconciling','unknown') LIMIT 3",(acct,)):
            results.append('旧立即测试 #%s (%s)'%(row[0],row[1]))
        row=d.execute('SELECT publish_times_json,account_ids_json FROM tt_post_auto_publish_config WHERE enabled=1').fetchone()
        if row and acct in map(str,load(row[1])) and local in load(row[0]):
            results.append('旧素材池固定时段')
        if d.execute("""SELECT 1 FROM social_random_slot WHERE target_id=? AND local_date=substr(datetime(?, '+8 hours'),1,10)
                       AND abs((julianday(planned_at)-julianday(?))*86400)<60 LIMIT 1""",(acct,at,at)).fetchone():
            results.append('旧素材池随机时段')
    return results


def at_local(at):
    from datetime import datetime
    from .pub_store import BEIJING
    return datetime.fromisoformat(at.replace('Z','+00:00')).astimezone(BEIJING).strftime('%H:%M')


def make_selector(native, store, spec, channel, seed, blocked=(), slot_key=''):
    from features.tt_auto_posts.selector import TwoStageSelector
    from .profiles import rank_candidates
    native_selector=native.selector
    source=native_selector.source
    exact=str(spec.get('content_id') or '')
    allowed=set(spec.get('material_ids') or [])
    if spec.get('material_id'):
        allowed={str(spec['material_id'])}
    # Resolve operator-supplied identities before traversing the drama catalog.
    parents=set()
    if allowed:
        for chunk in [sorted(allowed)[i:i+200] for i in range(0,len(allowed),200)]:
            marks=','.join('?' for _ in chunk).replace('?', '%s')
            sql=f"SELECT DISTINCT data_source_id FROM `{source.schema}`.ads_custom_source WHERE id IN ({marks})"
            parents.update(str(r['data_source_id']) for r in source._stream_select(sql,chunk))
    cutoff=now()-timedelta(days=spec.get('material_window_days',0))
    class Selector(TwoStageSelector):
        def _eligible_dramas(self,request,rows,*args):
            if exact or allowed:
                rows=[r for r in rows if (not exact or r.content_id==exact) and (not allowed or r.content_id in parents)]
            candidates=super()._eligible_dramas(request,rows,*args)
            self.ranking={}
            if spec.get('model')!='income' or not candidates:
                return candidates
            dto=[dict(content_id=r.content_id,language=r.language) for r in candidates]
            ranked,reason=rank_candidates(dto,channel.get('profile') or {},seed,source=source)
            self.ranking=reason
            lookup={r.content_id:r for r in candidates}
            return [lookup[r['content_id']] for r in ranked]
        def _eligible_materials(self,request,drama,rows,*args):
            if allowed:
                rows=[r for r in rows if r.material_id in allowed]
            if spec.get('material_window_days') and rows:
                sql=f"""SELECT CAST(id AS CHAR) AS material_id FROM `{source.schema}`.ads_custom_source
                         WHERE data_source=%s AND data_source_id=%s AND created_at>=FROM_UNIXTIME(%s)"""
                recent={str(v['material_id']) for v in source._stream_select(sql,(source.material_data_source,drama.content_id,int(cutoff.timestamp())))}
                rows=[r for r in rows if r.material_id in recent]
            return super()._eligible_materials(request,drama,rows,*args)
    history=OldHistory(native.legacy_reader,old_databases()[0],native.store.db_path,slot_key,blocked)
    return Selector(source,native_selector.metrics,history,store,
                    material_validator=native_selector.material_validator,product=native_selector.product,
                    app_id=native_selector.app_id,material_data_source=native_selector.material_data_source)


class Runtime:
    def __init__(self,path,env=None):
        from features.tt_auto_posts.service import build_service_from_env
        from features.tt_auto_posts.repositories import MetricWindowRepository
        path=str(Path(path).resolve())
        if any(Path(p).resolve()==Path(path) for p in old_databases()):
            raise PublicationError('新版与旧版账本路径冲突',500)
        source=dict(os.environ if env is None else env)
        source.update(TT_AUTO_POST_DB_PATH=path,TT_AUTO_POST_STATE_ROOT=str(Path(path).parent),
                      TT_AUTO_POST_EXECUTION_NOT_BEFORE_UTC='')
        self.native=build_service_from_env(source)
        self.store=self.native.store
        self.native.selector.metrics=MetricWindowRepository(MetricCache(old_databases()[0]))
        self.executor=promotion_executor(self.native,path)

    def conflicts(self,account,at):
        return conflicts(account,at)

    def settings(self,account):
        old=self.native.legacy_reader.get_account_setting(account)
        cfg=old.as_dict()
        for k in ('account_id','updated_at','version','drama_language'):
            cfg.pop(k,None)
        return cfg,old.version

    def preview(self,template,channel,opts,seed,blocked):
        from features.tt_auto_posts.service import _PreviewStore
        from features.tt_auto_posts.publisher import selector_rules
        from features.tt_auto_posts.selector import SelectionRequest
        config=template.config
        scoped={**config,**opts}
        selector=make_selector(self.native,_PreviewStore(self.store),scoped,channel,seed,blocked)
        rules=selector_rules(config)
        if opts.get('mode')=='serial':
            rules=replace(rules,drama=replace(rules.drama,cooldown_days=0))
        req=SelectionRequest(run_id=1,task_id=1,template_id=template.id,
            template_version=template.version,account_id=str(channel['account_id']),
            language=channel['drama_language'],rules=rules,now=now())
        raw=selector.select_and_reserve(req).as_dict()
        m=raw['material'];drama=raw['drama']
        ranking=getattr(selector,'ranking',{})
        if scoped.get('model')=='income':
            from .profiles import rank_candidates
            _, ranking=rank_candidates([dict(content_id=drama['content_id'],language=drama['language'])],channel.get('profile') or {},seed,source=self.native.selector.source)
        return dict(content_id=drama['content_id'],material_id=str(m['material_id']),
            drama_name=m.get('drama_name') or drama.get('name',''),description=m.get('description',''),
            material_name=m.get('material_name',''),decision=ranking,profile_version=(channel.get('profile') or {}).get('version',''),
            reason=(ranking.get('reason') or ranking.get('fallback_reason') or '频道收入画像') if ranking else '模板规则排序')

    def step(self):
        return self.executor.execute_next('tt-promotion-worker')


def promotion_executor(native,path):
    from features.tt_auto_posts.publisher import AutoPostExecutor,_ClaimedSelectionStore,AutoPostExecutionError
    from features.tt_auto_posts.links import build_auto_w2a_url,build_auto_short_url,write_auto_short_redirect,render_auto_caption
    from datetime import datetime
    class Executor(AutoPostExecutor):
        def frozen(self,task):
            with connection(path) as db:
                row=db.execute("""SELECT s.*,c.caption_body,c.source,v.config_json FROM ttp_slot s
                    JOIN ttp_job j ON j.id=s.job_id JOIN tt_auto_template_version v
                    ON v.template_id=j.template_id AND v.version=j.version
                    JOIN ttp_copy c ON c.slot_key=s.slot_key WHERE s.native_run_id=?""",(task.run_id,)).fetchone()
                if not row:
                    raise AutoPostExecutionError('tt_promotion_snapshot_missing','新版任务快照缺失',409)
                return dict(row)

        def _phase_selector(self,claim_token):
            # The native store passes each claim into an atomic TT material reservation.
            class FrozenSelector:
                def select_and_reserve(inner,request):
                    task=self.store.get_task(request.task_id);row=self.frozen(task)
                    spec=load(row['opts']);choice=load(row['choice'])
                    spec.update(content_id=choice['content_id'],material_id=choice['material_id'],model='rules',
                                material_window_days=load(row['config_json']).get('material_window_days',0))
                    if spec.get('mode')=='serial':
                        request=replace(request,rules=replace(request.rules,drama=replace(request.rules.drama,cooldown_days=0)))
                    sel=make_selector(native,_ClaimedSelectionStore(self.store,claim_token),spec,{},row['slot_key'],slot_key=row['slot_key'])
                    return sel.select_and_reserve(request)
            return FrozenSelector()

        def _caption_and_link(self,task):
            row=self.frozen(task);choice=load(row['choice'])
            stamp=task.reserved_at_utc or task.created_at
            ts=int(datetime.fromisoformat(str(stamp).replace('Z','+00:00')).timestamp())
            material=self._selection_material(task)
            drama_name=choice['drama_name']
            url=build_auto_w2a_url(link_id=task.id,username=task.account_username or task.account_id,
                timestamp=ts,language=task.drama_language,drama_name=drama_name or task.content_id,
                tag=material.get('material_tag') or 'TTauto', page_name=task.account_display_name or task.account_id,
                page_id=task.account_id,material_name=material.get('material_name') or task.material_id,
                material_id=task.material_id,content_id=task.content_id)
            write_auto_short_redirect(self.short_link_root,task.id,url)
            code=self.code_route_broker.freeze_route(task.id,content_id=task.content_id,long_url=url,created_at=str(stamp))
            tmpl=row['caption_body'] or load(row['config_json'])['caption_template']
            caption=render_auto_caption(tmpl,task.content_id,short_url=build_auto_short_url(task.id),
                m_url=None,description=choice.get('description') or drama_name,code=code,drama_name=drama_name)
            if task.caption and task.caption!=caption:
                raise AutoPostExecutionError('tt_promotion_caption_conflict','任务文案与定稿不一致',409)
            if choice.get('code')!=code:
                with connection(path,write=True) as db:
                    db.execute("UPDATE ttp_slot SET choice=json_set(choice,'$.code',?) WHERE slot_key=?",(code,row['slot_key']))
            return caption

        def _sync_code_state(self,task,state,*,best_effort=False):
            try:
                self.code_route_broker.set_state(task.id,state=state,updated_at=iso(now()))
            except Exception:
                if not best_effort: raise

        def _publish(self,task,claim_token=None):
            if not task.publish_id and not task.unknown_outcome:
                hits=conflicts(task.account_id,task.scheduled_at_utc)
                if hits or self.selector.legacy_reader.seen_material_ids([task.material_id]):
                    raise AutoPostExecutionError('tt_promotion_legacy_conflict','原任务存在排期或素材冲突',409)
                with connection(old_databases()[0]) as db:
                    if db.execute('SELECT 1 FROM tt_auto_material_ledger WHERE material_id=?',(task.material_id,)).fetchone():
                        raise AutoPostExecutionError('tt_promotion_legacy_material','素材已进入旧自动模板账本',409)
            return super()._publish(task,claim_token)
    executor=Executor.__new__(Executor)
    executor.__dict__.update(native.executor.__dict__)
    return executor
