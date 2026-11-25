"""Operations for TT Promotion. All commands are scoped to the independent ledger."""
import argparse
import hashlib
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta

from features.tt_auto_posts.core import AuditActor
from . import pub_copy
from .pub_native import PublicationError,Runtime,number
from .pub_store import BEIJING,connection,dump,install,iso,load,now

DEFAULT_DB='/mnt/data-disk/tt-promotion/promotion.sqlite3'
TERMINAL={'published','failed','no_candidate','canceled','cancelled','skipped','missed'}


def audit(actor):
    return AuditActor.from_values(actor['user_id'],actor.get('name') or actor.get('user_name') or actor['user_id'])


def identity(actor):
    return {k:str(actor.get(k) or '') for k in ('user_id','name','tenant_key','role')}


def clean_ids(value,label,pattern=r'[1-9][0-9]{0,30}',maximum=200):
    if not isinstance(value,list) or not 1<=len(value)<=maximum:
        raise PublicationError(label+'数量无效')
    out=[str(i).strip() for i in value]
    if len(set(out))!=len(out) or any(not re.fullmatch(pattern,i) for i in out):
        raise PublicationError(label+'无效或重复')
    return out


def safe_caption(raw):
    if not isinstance(raw,str) or not raw.strip() or len(raw.encode('utf-16-le'))//2>2200:
        raise PublicationError('文案不能为空或超过平台上限')
    text=raw.strip()
    for token in ('{{drama_name}}','{{content_id}}','{{contect_id}}','{desc}','{url}','{code}'):
        raw=raw.replace(token,'')
    if '{' in raw or '}' in raw:
        raise PublicationError('文案含不支持的推广宏')
    return text


def config_for(payload,visible,actor,creator=None):
    name=str(payload.get('name') or '').strip()
    if not name or len(name)>120: raise PublicationError('模板名称长度应为1到120个字符')
    ids=clean_ids(payload.get('account_ids'),'频道')
    if not set(ids)<=visible:raise PublicationError('所选频道超出可见范围',403)
    sch=payload.get('schedule')
    if not isinstance(sch,dict):raise PublicationError('请选择排期')
    if sch.get('mode')=='fixed':
        times=clean_ids(sch.get('times'),'发布时间',r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',24)
        sch={'mode':'fixed','times':sorted(times)}
    elif sch.get('mode')=='random':
        sch={'mode':'random','daily_count':number(sch.get('daily_count'),1,24,'随机发布次数')}
    else:
        raise PublicationError('排期模式无效')
    model=payload.get('model','income');video=payload.get('video_template','random_overlay')
    if model not in ('income','rules') or video not in ('random_overlay','direct_outro') or type(payload.get('ai_copy',False))!=bool:
        raise PublicationError('选材、制作或文案设置无效')
    base={'spend_min':None,'spend_max':None,'roas_min':None,'roas_max':None,'sort_by':'roas','sort_direction':'desc'}
    return name,dict(account_ids=ids,schedule=sch,model=model,video_template=video,ai_copy=payload.get('ai_copy',False),
        metric_window_days=number(payload.get('metric_window_days',7),1,30,'指标窗口'),
        drama_launch_window_days=number(payload.get('drama_launch_window_days',60),0,3650,'剧目窗口'),
        material_window_days=number(payload.get('material_window_days',30),0,3650,'素材窗口'),
        cooldown_days=number(payload.get('cooldown_days',0),0,3650,'同剧冷却'),
        caption_template=safe_caption(payload.get('caption_template','{desc}\n{code}')),
        platform=0,drama_rule={**base,'resource_type_v2':[]},
        material_rule={**base,'duration_min_seconds':0,'duration_max_seconds':3600},
        creator=creator or identity(actor))


def when(payload):
    value=payload.get('scheduled_at')
    if not value:return iso(now())
    try: date=datetime.fromisoformat(str(value).replace('Z','+00:00'));at=iso(date)
    except (ValueError,TypeError):raise PublicationError('预约时间无效') from None
    if date<now()-timedelta(minutes=1):raise PublicationError('预约时间已过')
    return at


def options(payload):
    mode=payload.get('mode','template')
    if mode not in ('template','drama','material','pool','serial'):raise PublicationError('推广方式无效')
    result={'mode':mode}
    if mode in ('drama','serial'):
        ident=str(payload.get('content_id') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',ident):raise PublicationError('请填写有效剧目 ID')
        result['content_id']=ident
    if mode=='material':
        result['material_id']=clean_ids([payload.get('material_id')],'素材')[0]
    if mode=='pool' and payload.get('material_ids'):
        result['material_ids']=clean_ids(payload['material_ids'],'素材池',maximum=1000)
    if mode=='serial' and payload.get('episodes'):
        result['episodes']=clean_ids(payload['episodes'],'剧集素材',maximum=500)
    if payload.get('manual_caption'):
        result['manual_caption']=safe_caption(payload['manual_caption'])
    return result


def public(template):
    return dict(id=template.id,name=template.name,version=template.version,enabled=template.enabled,
                config={k:v for k,v in template.config.items() if k!='creator'},updated_at=template.updated_at)


def visible(template,actor,channels):
    owner=template.config.get('creator') or {}
    return (bool(owner.get('tenant_key')) and owner['tenant_key']==actor.get('tenant_key')
            and set(template.config['account_ids'])<=set(channels))


def summarize(items):
    states=[i['status'] for i in items]
    if states and all(s=='published' for s in states):return 'published'
    if any(s in ('unknown','reconciling') for s in states):return 'needs_review'
    if states and all(s in TERMINAL for s in states):return 'completed_with_errors'
    return 'running' if any(s not in ('planned','enqueued') for s in states) else 'planned'


def day_slots(template,account,date):
    schedule=template.config['schedule']
    if schedule['mode']=='fixed':times=schedule['times']
    else:
        seed=f'{template.id}/{template.version}/{account}/{date}'
        rng=random.Random(seed)
        count=schedule['daily_count'];width=1440//count
        minutes=[i*width+rng.randrange(1,width) for i in range(count)]
        times=[f'{m//60:02d}:{m%60:02d}' for m in minutes]
    return [iso(datetime.fromisoformat(date+'T'+v).replace(tzinfo=BEIJING)) for v in times]


class Publisher:
    def __init__(self,db_path=DEFAULT_DB,runtime=None):
        self.path=str(db_path)
        self.runtime=runtime or Runtime(self.path)
        self.store=self.runtime.store
        install(self.path)

    @staticmethod
    def channel_map(channels):
        return {str(c['account_id']):dict(c,drama_language=c.get('drama_language') or c.get('language') or '') for c in channels}

    def handle(self,method,path,payload,actor,channels):
        payload=dict(payload or {})
        scope=self.channel_map(channels)
        parts=path.strip('/').split('/')
        if not actor.get('tenant_key') or not actor.get('user_id'):raise PublicationError('请先登录',401)
        if parts==['runs'] and method=='GET':return self.runs(actor,scope)
        if len(parts)==2 and parts[0]=='runs' and parts[1].isdigit() and method=='GET':return self.run_detail(int(parts[1]),actor,scope)
        if parts[0]!='templates':raise PublicationError('没有此操作',404)
        if len(parts)==1:
            if method=='GET':return {'items':[public(i) for i in self.store.list_templates() if visible(i,actor,scope)]}
            if method=='POST':
                name,cfg=config_for(payload,set(scope),actor)
                return public(self.store.create_template(name=name,config=cfg,actor=audit(actor),confirmation={'accepted':True}))
        if len(parts)<2 or not parts[1].isdigit():raise PublicationError('模板无效',404)
        t=self.store.get_template(int(parts[1]))
        if not visible(t,actor,scope):raise PublicationError('模板不存在或无权限',404)
        if len(parts)==2 and method=='GET':return public(t)
        if len(parts)==4 and parts[2]=='draft' and method=='GET':
            return self.draft(t,actor,scope,parts[3])
        if method not in ('PUT','POST'):raise PublicationError('操作无效',405)
        if type(payload.get('expected_version'))!=int or payload['expected_version']!=t.version:
            raise PublicationError('模板已更新，请刷新',409)
        if len(parts)==2:
            name,cfg=config_for(payload,set(scope),actor,t.config['creator'])
            return public(self.store.update_template(t.id,expected_version=t.version,name=name,config=cfg,
                    actor=audit(actor),confirmation={'accepted':True}))
        action=parts[2] if len(parts)==3 else ''
        if action in ('enable','disable'):
            result=self.store.set_template_enabled(t.id,action=='enable',expected_version=t.version,actor=audit(actor))
            if action=='disable':
                with connection(self.path,write=True) as db:
                    db.execute("""UPDATE ttp_slot SET state='skipped',reason='模板已暂停' WHERE state='planned' AND
                        job_id IN (SELECT id FROM ttp_job WHERE template_id=? AND trigger_type='auto')""",(t.id,))
            return public(result)
        if action=='draft':
            return self.start_draft(t,actor,scope,payload)
        opts=options(payload)
        ids=clean_ids(payload.get('account_ids',t.config['account_ids']),'频道')
        if not set(ids)<=set(t.config['account_ids']):raise PublicationError('频道不属于此模板',403)
        if action=='preview':
            if payload.get('plan_date'):
                return self.preview_day(t,scope,ids,opts,payload)
            rows=self.preview(t,scope,ids,opts,when(payload))
            return {'items':rows,'total':len(rows),'offset':0,'limit':len(rows)}
        if action=='run':
            if opts['mode']=='serial': ids=self._series_order(t,scope,ids,opts)
            op=str(payload.get('operation_id') or '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',op):raise PublicationError('缺少操作标识')
            # Scheduled time is omitted from the hash only for immediate requests.
            stamp=when(payload) if not self._already(op,actor) else None
            key='manual:'+str(actor['tenant_key'])+':'+str(actor['user_id'])+':'+op
            digest=dump([t.id,t.version,ids,opts,payload.get('scheduled_at')])
            job_id=self.plan(t,actor,[(i,stamp) for i in ids],opts,key,digest,'manual')
            return self.run_detail(job_id,actor,scope)
        raise PublicationError('没有此操作',404)

    def draft(self,t,actor,scope,key):
        with connection(self.path) as db:
            r=db.execute('SELECT * FROM ttp_draft WHERE slot_key=? AND actor_id=? AND template_id=?',
                         (key,actor['user_id'],t.id)).fetchone()
        if not r or r['account_id'] not in scope:raise PublicationError('未找到预演文案',404)
        return dict(id=r['slot_key'],state=r['state'],source=r['source'],reason=r['reason'],caption=r['caption_body'],
                    account_id=r['account_id'],material_id=r['material_id'],template_version=r['template_version'])

    def start_draft(self,t,actor,scope,payload):
        account=clean_ids([payload.get('account_id')],'频道')[0]
        mid=clean_ids([payload.get('material_id')],'素材')[0]
        if account not in t.config['account_ids']:raise PublicationError('频道不属于模板',403)
        op=str(payload.get('operation_id') or '')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}',op):raise PublicationError('缺少操作标识')
        key=hashlib.sha256(dump([actor['tenant_key'],actor['user_id'],t.id,t.version,account,mid,op]).encode()).hexdigest()
        with connection(self.path) as db:
            exists=db.execute('SELECT 1 FROM ttp_draft WHERE slot_key=?',(key,)).fetchone()
        if not exists:
            ch=scope[account]
            if not ch.get('eligible') or not ch.get('enabled'):raise PublicationError('频道不可用',409)
            choice=self.runtime.preview(t,ch,{'mode':'material','material_id':mid},key,())
            if str(choice['material_id'])!=mid:raise PublicationError('素材与请求不一致',409)
            facts=pub_copy.draft_facts(key,ch,choice,t.config['caption_template'])
            with connection(self.path,write=True) as db:
                db.execute('INSERT OR IGNORE INTO ttp_draft(slot_key,template_id,template_version,actor_id,account_id,material_id,created_at,facts) VALUES(?,?,?,?,?,?,?,?)',
                           (key,t.id,t.version,actor['user_id'],account,mid,iso(now()),dump(facts)))
        return self.draft(t,actor,scope,key)

    def _already(self,op,actor):
        with connection(self.path) as db:
            return db.execute('SELECT 1 FROM ttp_job WHERE op_key=?',
                              ('manual:'+str(actor['tenant_key'])+':'+str(actor['user_id'])+':'+op,)).fetchone()

    def plan(self,t,actor,pairs,opts,key,facts,trigger):
        stamp=iso(now());digest=hashlib.sha256(facts.encode()).hexdigest()
        with connection(self.path,write=True) as db:
            row=db.execute('SELECT id,request_hash FROM ttp_job WHERE op_key=?',(key,)).fetchone()
            if row:
                if row[1]!=digest:raise PublicationError('操作标识已绑定其他请求',409)
                return row[0]
            version=db.execute('SELECT current_version,enabled FROM tt_auto_template WHERE id=?',(t.id,)).fetchone()
            if (trigger=='manual' and version[0]!=t.version) or (trigger=='auto' and not version[1]):raise PublicationError('模板已更新',409)
            job=db.execute('INSERT INTO ttp_job(op_key,request_hash,template_id,version,template_name,tenant,actor,trigger_type,opts,created_at)'
                 ' VALUES(?,?,?,?,?,?,?,?,?,?)',(key,digest,t.id,t.version,t.name,actor['tenant_key'],dump(identity(actor)),trigger,dump(opts),stamp)).lastrowid
            if opts.get('mode')=='pool':
                for m in opts.get('material_ids',[]):
                    db.execute('INSERT OR IGNORE INTO ttp_pool VALUES(?,?)',(t.id,m))
            for acct,at in pairs:
                # One new owner per account/minute. Older native run IDs remain separate.
                dup=db.execute("""SELECT slot_key FROM ttp_slot WHERE account_id=? AND
                    abs((julianday(scheduled_at)-julianday(?))*86400)<60 AND state NOT IN ('skipped','missed') LIMIT 1""",(acct,at)).fetchone()
                state,reason=('skipped','与新版已有排期重叠') if dup else ('planned','')
                db.execute('INSERT INTO ttp_slot(slot_key,job_id,account_id,scheduled_at,state,reason,opts,changed_at) '
                    'VALUES(?,?,?,?,?,?,?,?)',(str(job)+':'+acct,job,acct,at,state,reason,dump(opts),stamp))
            return job

    def _spec(self,db,t,acct,opts):
        opts=dict(opts)
        if opts['mode']=='pool':
            added=set(opts.get('material_ids') or [])
            added.update(r[0] for r in db.execute('SELECT material_id FROM ttp_pool WHERE template_id=?',(t.id,)))
            if not added:raise PublicationError('素材池暂无素材',409)
            opts['material_ids']=sorted(added)
        if opts['mode']=='template':
            for row in db.execute('SELECT * FROM ttp_series WHERE template_id=? AND account_id=? ORDER BY rowid',(t.id,acct)):
                episodes=load(row['episodes'])
                if not self._series_state(db,row,episodes)[1]:
                    opts.update(mode='serial',content_id=row['content_id'],episodes=episodes)
                    break
        if opts['mode']=='serial':
            row=db.execute('SELECT * FROM ttp_series WHERE tenant=? AND content_id=?',(t.config['creator']['tenant_key'],opts['content_id'])).fetchone()
            episodes=list(opts.get('episodes') or (load(row['episodes']) if row else []))
            if not episodes:raise PublicationError('请按集序提供素材 ID',409)
            if row and (row['account_id']!=acct or row['template_id']!=t.id or episodes!=load(row['episodes'])):
                raise PublicationError('短剧已绑定其他频道或集序',409)
            idx,complete=self._series_state(db,row,episodes)
            if complete:raise PublicationError('该连载已发完',409)
            opts.update(episodes=episodes,episode=idx+1,material_id=episodes[idx])
        return opts

    @staticmethod
    def _series_state(db,row,episodes):
        if not row:return 0,False
        for idx,m in enumerate(episodes):
            state=db.execute("""SELECT s.opts,s.account_id,j.tenant,j.template_id,t.status,t.publish_id,t.unknown_outcome
                FROM ttp_slot s JOIN ttp_job j ON j.id=s.job_id LEFT JOIN tt_auto_task t ON t.run_id=s.native_run_id
                AND t.account_id=s.account_id WHERE s.material_id=?""",(m,)).fetchone()
            if not state:return idx,False
            opts=load(state['opts'])
            if (state['tenant']!=row['tenant'] or state['template_id']!=row['template_id']
                    or state['account_id']!=row['account_id'] or opts.get('mode')!='serial'
                    or opts.get('content_id')!=row['content_id'] or opts.get('episode')!=idx+1):
                raise PublicationError('连载素材已被其他任务占用，请核对该集',409)
            if state['status']!='published' or not state['publish_id'] or state['unknown_outcome']:
                raise PublicationError('上一集尚未确认发布或需要核对',409)
        return len(episodes),True

    def sample(self,t,channel,opts,at,key,blocked=(),*,inspect=False):
        acct=str(channel['account_id'])
        row=dict(account_id=acct,scheduled_at_utc=at,status='skipped',reason='',conflicts=[],caption='')
        if not channel.get('eligible') or not channel.get('enabled') or not channel.get('drama_language'):
            return dict(row,reason=channel.get('reason') or channel.get('eligibility_reason') or '频道未参与、不可发布或尚未设置剧语言')
        self.runtime.settings(channel)
        hits=self.runtime.conflicts(acct,at)
        if hits and not inspect:return dict(row,reason='；'.join(hits),conflicts=hits)
        with connection(self.path) as db:
            opts=self._spec(db,t,acct,opts)
        choice=self.runtime.preview(t,channel,opts,key,blocked)
        opts_caption=opts.get('manual_caption') or t.config['caption_template']
        sample=opts_caption
        for k,v in {'{{drama_name}}':choice['drama_name'],'{{content_id}}':choice['content_id'],
                    '{{contect_id}}':choice['content_id'],'{desc}':choice.get('description') or choice['drama_name'],
                    '{code}':'〔正式四位码〕','{url}':'〔正式短链〕'}.items():
            sample=sample.replace(k,v)
        return dict(row,**{k:v for k,v in choice.items() if k not in row},status='skipped' if hits else 'eligible',conflicts=hits,
                    reason='；'.join(hits) if hits else choice['reason'],caption=sample,options=opts)

    def preview(self,t,scope,ids,opts,at):
        items=[];blocked=set();bound=None
        if opts['mode']=='serial':
            ids=self._series_order(t,scope,ids,opts)
        for acct in ids:
            try:
                if bound:raise PublicationError('此次连载分配到频道 '+bound,409)
                item=self.sample(t,scope[acct],opts,at,f'{t.id}:{t.version}:{acct}:{at[:10]}',blocked,inspect=True)
                if item['status']=='eligible':
                    blocked.add(item['material_id'])
                    if opts['mode']=='serial':bound=acct
            except Exception as e:
                item=dict(account_id=acct,scheduled_at_utc=at,status='skipped',reason=error_text(e),conflicts=[])
            items.append({k:v for k,v in item.items() if k!='options'})
        return items

    def preview_day(self,t,scope,ids,opts,payload):
        try:
            date=datetime.strptime(str(payload['plan_date']),'%Y-%m-%d').date().isoformat()
        except ValueError:raise PublicationError('日排期预演日期无效') from None
        limit=number(payload.get('limit',8),1,24,'每页数量')
        offset=number(payload.get('offset',0),0,100000,'分页')
        with connection(self.path) as db:
            frozen=db.execute('SELECT version,slots FROM ttp_day WHERE template_id=? AND day=?',(t.id,date)).fetchone()
        if frozen:
            t=self.store.get_template(t.id,version=frozen['version'])
            pairs=[p for p in load(frozen['slots']) if p[0] in ids]
        else:
            pairs=[(acct,at) for acct in ids for at in day_slots(t,acct,date)]
        pairs.sort(key=lambda p:(p[1],p[0]))
        result=[];seen=set()
        for acct,at in pairs[offset:offset+limit]:
            try:
                item=self.sample(t,scope[acct],opts,at,f'{t.id}:{t.version}:{acct}:{date}',seen,inspect=True)
                if item['status']=='eligible':seen.add(item['material_id'])
            except Exception as e:
                item=dict(account_id=acct,scheduled_at_utc=at,status='skipped',reason=error_text(e),conflicts=[])
            result.append({k:v for k,v in item.items() if k!='options'})
        return dict(items=result,total=len(pairs),limit=limit,offset=offset,template_version=t.version)

    def _series_order(self,t,scope,ids,opts):
        from .profiles import match_drama
        source = getattr(getattr(getattr(self.runtime,'native',None),'selector',None),'source',None)
        def score(acct):
            return match_drama(scope[acct].get('profile') or {},opts['content_id'],scope[acct]['drama_language'],source=source)
        return sorted(ids,key=score,reverse=True)

    def runs(self,actor,scope):
        with connection(self.path) as db:
            rows=db.execute('SELECT id FROM ttp_job WHERE tenant=? ORDER BY id DESC LIMIT 200',(actor['tenant_key'],)).fetchall()
        items=[]
        for row in rows:
            try: item=self.run_detail(row['id'],actor,scope)
            except PublicationError:continue
            item['count']=len(item.pop('items'));items.append(item)
        return {'items':items}

    def run_detail(self,job,actor,scope):
        with connection(self.path) as db:
            j=db.execute('SELECT * FROM ttp_job WHERE id=? AND tenant=?',(job,actor['tenant_key'])).fetchone()
            if not j:raise PublicationError('运行记录不存在',404)
            rows=db.execute("""SELECT s.*,t.id task_id,t.status native_status,t.publish_id,t.publish_url,p.video_ids,
                       t.error_code,t.error_message,t.caption,t.content_id,t.material_id native_material,
                       c.source copy_source,c.reason copy_reason
                FROM ttp_slot s LEFT JOIN tt_auto_task t ON t.run_id=s.native_run_id AND t.account_id=s.account_id
                LEFT JOIN ttp_copy c ON c.slot_key=s.slot_key LEFT JOIN ttp_public_receipt p ON p.task_id=t.id WHERE s.job_id=? ORDER BY s.rowid""",(job,)).fetchall()
        if not all(r['account_id'] in scope for r in rows):raise PublicationError('运行记录不存在',404)
        items=[]
        for r in rows:
            ch=load(r['choice'])
            items.append(dict(id=r['task_id'],account_id=r['account_id'],status=r['native_status'] or r['state'],
                scheduled_at_utc=r['scheduled_at'],reason=r['error_message'] or r['reason'] or ch.get('reason',''),
                error_code=r['error_code'],content_id=r['content_id'] or ch.get('content_id',''),
                material_id=r['native_material'] or r['material_id'],drama_name=ch.get('drama_name',''),
                decision=ch.get('decision'),profile_version=ch.get('profile_version'),caption=r['caption'] or ch.get('caption',''),
                copy_source=r['copy_source'],copy_reason=r['copy_reason'],code=ch.get('code'),publish_id=r['publish_id'],
                publish_url=r['publish_url'],public_video_ids=load(r['video_ids']) if r['video_ids'] else []))
        return dict(id=j['id'],template_id=j['template_id'],template_name=j['template_name'],version=j['version'],
             status=summarize(items),trigger_type=j['trigger_type'],created_at=j['created_at'],
             scheduled_at_utc=min((r['scheduled_at'] for r in rows),default=None),items=items)


    def schedule(self):
        current=now()
        today=current.astimezone(BEIJING).date().isoformat()
        for t in self.store.list_templates(enabled=True):
            creator=t.config.get('creator') or {}
            if not creator.get('tenant_key'):continue
            with connection(self.path,write=True) as db:
                record=db.execute("SELECT * FROM ttp_day WHERE template_id=? AND day=?",(t.id,today)).fetchone()
                if not record:
                    slots=[(acct,at) for acct in t.config['account_ids'] for at in day_slots(t,acct,today)
                           if at>=iso(datetime.fromisoformat(t.enabled_at_utc.replace('Z','+00:00')))]
                    db.execute("INSERT INTO ttp_day VALUES(?,?,?,?)",(t.id,today,t.version,dump(slots)))
                    version,pairs=t.version,slots
                else:
                    version,pairs=record['version'],load(record['slots'])
            frozen=self.store.get_template(t.id,version=version)
            for acct,at in pairs:
                key=f'auto:{t.id}:{acct}:{at}'
                self.plan(frozen,creator,[(acct,at)],{'mode':'template'},key,key,'auto')

    def work(self,channels_for,*,limit=32):
        self.schedule()
        current=now()
        with connection(self.path) as db:
            slots=db.execute("""SELECT s.*,j.template_id,j.version,j.actor,j.trigger_type FROM ttp_slot s
                JOIN ttp_job j ON j.id=s.job_id WHERE s.state IN ('planned','reserved') AND s.next_at<=?
                AND s.scheduled_at<=? ORDER BY s.scheduled_at,s.job_id,s.rowid LIMIT ?""",
                (iso(current),iso(current+timedelta(minutes=45)),limit)).fetchall()
        scopes={}
        for row in slots:
            try:
                creator=load(row['actor'])
                if row['actor'] not in scopes:
                    scopes[row['actor']]=self.channel_map(channels_for(creator))
                ch=scopes[row['actor']].get(row['account_id'])
                if not ch or not ch.get('enabled') or not ch.get('eligible'):
                    raise PublicationError('频道不在可发布范围或未参与',409)
                self._advance(row,ch,current)
            except Exception as e:
                retry=is_transient(e) and current<datetime.fromisoformat(row['scheduled_at'])+timedelta(minutes=20)
                with connection(self.path,write=True) as db:
                    db.execute("""UPDATE ttp_slot SET state=?,reason=?,next_at=?,changed_at=?
                            WHERE slot_key=? AND state=?""",
                        (row['state'] if retry else 'skipped',error_text(e),iso(current+timedelta(seconds=45)),
                         iso(current),row['slot_key'],row['state']))
        return self.runtime.step()

    def _advance(self,s,ch,current):
        t=self.store.get_template(s['template_id'],version=s['version'])
        if s['state']=='planned':
            if s['trigger_type']=='auto' and current>datetime.fromisoformat(s['scheduled_at'])+timedelta(minutes=20):
                raise PublicationError('计划时段已过',409)
            item=self.sample(t,ch,load(s['opts']),s['scheduled_at'],
                f"{t.id}:{t.version}:{ch['account_id']}:{s['scheduled_at'][:10]}")
            if item['status']!='eligible':raise PublicationError(item['reason'],409)
            opts=item.pop('options')
            settings,setting_version=self.runtime.settings(ch)
            acct=dict(settings=settings,version=setting_version,drama_language=ch['drama_language'],
                      username=ch.get('username') or ch.get('account_username') or '',
                      display_name=ch.get('nickname') or ch.get('display_name') or ch.get('name') or '')
            with connection(self.path,write=True) as db:
                if db.execute('SELECT state FROM ttp_slot WHERE slot_key=?',(s['slot_key'],)).fetchone()[0]!='planned':return
                if opts.get('mode')=='serial':
                    db.execute('INSERT OR IGNORE INTO ttp_series VALUES(?,?,?,?,?)',(t.config['creator']['tenant_key'],
                        opts['content_id'],t.id,s['account_id'],dump(opts['episodes'])))
                    self._spec(db,t,s['account_id'],opts)
                try:
                    count=db.execute("""UPDATE ttp_slot SET state='reserved',material_id=?,choice=?,account_json=?,opts=?,
                                changed_at=? WHERE slot_key=? AND state='planned'""",
                        (item['material_id'],dump(item),dump(acct),dump(opts),iso(current),s['slot_key'])).rowcount
                except sqlite3.IntegrityError:
                    raise PublicationError('该素材已被其他任务预约',409) from None
                if count:pub_copy.submit(db,s['slot_key'],ch,item,t.config,opts.get('manual_caption'),episode=opts.get('episode'))
            return
        if self.runtime.conflicts(s['account_id'],s['scheduled_at']):
            raise PublicationError('旧排期或原发布任务占用该频道',409)
        with connection(self.path,write=True) as db:
            c=db.execute('SELECT * FROM ttp_copy WHERE slot_key=?',(s['slot_key'],)).fetchone()
            if not c:raise PublicationError('任务文案尚未就绪',503)
            if c['state']!='final':
                deadline=max(datetime.fromisoformat(s['scheduled_at']),datetime.fromisoformat(s['changed_at'])+timedelta(seconds=90))
                if current<deadline:return
                pub_copy.fallback(db,s['slot_key'],'发布截止时间到达')
        actor=load(s['actor']);frozen=load(s['account_json'])
        run_key='promotion:'+s['slot_key']
        run=self.store.get_run_by_key(run_key)
        if not run:
            local=datetime.fromisoformat(s['scheduled_at']).astimezone(BEIJING)
            # The promotion planner owns its frozen day/slot and eligibility.
            # Native runs are isolated commands. Reports use the frozen trigger above.
            run=self.store.create_run(run_key=run_key,template_id=t.id,template_version=t.version,
                trigger_type='manual',scheduled_at_utc=s['scheduled_at'],shanghai_date=local.date().isoformat(),
                publish_time=local.strftime('%H:%M'),actor=audit(actor))
        with connection(self.path,write=True) as db:
            db.execute("UPDATE ttp_slot SET native_run_id=? WHERE slot_key=? AND state='reserved'",(run.id,s['slot_key']))
        self.store.create_task(run_id=run.id,account_id=s['account_id'],account_username=frozen['username'],
            account_display_name=frozen['display_name'],drama_language=frozen['drama_language'],
            account_settings=frozen['settings'],account_setting_version=frozen['version'])
        with connection(self.path,write=True) as db:
            db.execute("UPDATE ttp_slot SET state='enqueued',changed_at=? WHERE slot_key=? AND state='reserved'",
                       (iso(current),s['slot_key']))


def is_transient(e):
    return int(getattr(e,'status',503) or 503)>=500 or getattr(e,'code','') in ('tt_auto_metric_window_not_ready',)


def error_text(e):
    if isinstance(e,PublicationError):return str(e)[:300]
    return str(getattr(e,'code','') or type(e).__name__)[:160]


def initialize(path):
    from features.tt_auto_posts.core import TTPostAutoStore
    from .pub_native import old_databases
    from pathlib import Path
    if Path(path).resolve() in {Path(p).resolve() for p in old_databases()}:
        raise PublicationError('新版数据库不能指向旧发布器',500)
    TTPostAutoStore(path)
    install(path)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['init','tick'])
    args=parser.parse_args()
    path=os.environ.get('TT_PROMOTION_DB',DEFAULT_DB)
    if args.command=='init':
        initialize(path)
        return
    from .channels import worker_channels
    service=Publisher(path)
    report=service.work(worker_channels)
    # The native wire-format also has operational details unnecessary for journal output.
    print(dump({'claimed':bool(report.get('claimed'))}))


if __name__=='__main__':
    main()
