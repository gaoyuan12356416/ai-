"""Trusted Sidecar adapter between browser DTOs and durable publishing state."""
import os
import re
from features.x_posts.service import XPostError
from .core import PersonalStore
from .errors import PublishingError
from .sources import LANGUAGES, SourceClient, safe_url
from .transport import broker

def processing(value):
    if value in (None,'','original'):
        return 'original'
    if value in ('random','random_overlay'):
        return 'random'
    raise PublishingError('invalid_processing','加工方式无效',400)

def client_processing(value):
    return 'random_overlay' if value=='random' else 'original'

def batch_dto(batch, *, include_items=True):
    status_map={'waiting':'queued','processing':'processing','success':'published','needs_review':'unknown',
                'completed_with_errors':'partial','running':'processing','stopped':'stopped'}
    result = {'batch_id':str(batch['id']),'source':batch['source'],'status':status_map.get(batch['status'],batch['status']),
              'blocked_reason':batch.get('blocked_reason') or '', 'created_at_utc':batch['created_at'],
              'total_tasks':len(batch.get('tasks',[])),'created':bool(batch.get('created'))}
    if not include_items:
        return result
    items=[]
    for t in batch.get('tasks',[]):
        material=t.get('source_snapshot') or {}; account=t.get('account_snapshot') or {}
        items.append({'task_id':str(t['id']),'material_id':str(t['material_id']),
            'material_name':material.get('material_name') or material.get('drama_name') or str(t['material_id']),
            'account_id':str(t['account_id']),'account_name':account.get('display_name') or account.get('username') or str(t['account_id']),
            'x_user_id':str(account.get('x_user_id') or ''),
            'caption':t.get('post_text') or t.get('caption') or '',
            'planned_at_utc':t.get('scheduled_at') or None,'processing_mode':client_processing(t.get('processing')),
            'schedule_kind':'scheduled' if t.get('scheduled_at') else 'immediate',
            'status':status_map.get(t['status'],t['status']),'error_message':t.get('error_message') or '',
            'post_url':safe_url(t.get('x_post_url')),'row_number':t.get('row_number'),
            'queue_id':str(t.get('queue_id') or ''),'manual_run_id':str(t.get('manual_run_id') or '')})
    result['items'] = items
    return result

class PublishingService:
    def __init__(self,oauth,*,source=None,identity=None,enabled=None):
        self.oauth=oauth
        self.store=PersonalStore(oauth.POST_DB_PATH)
        self.source=source or SourceClient(oauth.INTERNAL_TOKEN)
        self.identity=identity or (lambda tenant,user:broker('identity',{'tenant_key':tenant,'user_id':user},oauth.INTERNAL_TOKEN)['actor'])
        self.enabled=(os.environ.get('X_VIDEO_PUBLISH_ENABLED','0')=='1') if enabled is None else enabled

    def account(self,actor,account_id):
        if not re.fullmatch(r'[1-9][0-9]{0,18}',str(account_id)):
            raise PublishingError('invalid_account','账号 ID 无效',400)
        return self.oauth.row_to_item(self.oauth.find_scoped_account_row(int(account_id),actor,'platform' if actor.get('x_platform_access') is True else 'mine'))

    def accounts(self,actor):
        return {str(a['id']):a for a in self.oauth.list_accounts(actor,'platform' if actor.get('x_platform_access') is True else 'mine')['items']}

    def account_dto(self,actor,account):
        config=self.store.get_settings(actor,str(account['id']))
        eligible=account.get('status')=='active' and account.get('publish_approved') is True and account.get('publish_eligible') is True
        return {'account_id':str(account['id']),'x_user_id':str(account.get('x_user_id') or ''),
                'display_name':account.get('display_name') or account.get('username') or str(account['id']),
                'username':account.get('username') or '', 'profile_image_url':safe_url(account.get('profile_image_url')),
                'language':account.get('drama_language') or '', 'status':account.get('status') or 'unknown',
                'publish_approved':account.get('publish_approved') is True,'eligible':eligible,
                'blocked_reason':'' if eligible else '账号授权不可用或未允许发布',
                'caption_template':config['default_copy'],'config_version':config['version'],
                'daily_auto_publish_configured':account.get('daily_auto_publish_configured') is True,
                'public_metrics':account.get('public_metrics') or {},'operating_stats':{}}

    def _pool(self):
        from .pool import PoolService
        def call(kind, callback, *args):
            try:
                return callback(*args)
            except Exception as exc:
                # Scheduler classification uses the durable publisher's error
                # type. Keep network/permission failures out of generic crashes.
                oauth_error=getattr(self.oauth,'ServiceError',())
                if isinstance(exc,XPostError):
                    raise
                if isinstance(exc,PublishingError) or isinstance(exc,oauth_error):
                    code='account_not_found' if kind=='account' and exc.status==404 else exc.code
                    raise XPostError(code,str(exc),exc.status) from None
                raise
        return PoolService(self.store,
            lambda actor,aid:call('account',self.account,actor,aid),
            lambda actor,mids:call('source',self.source.resolve,mids),
            lambda tenant,user:call('identity',self.identity,tenant,user),enabled=self.enabled)

    def preview(self,actor,payload,*,excel=False):
        accounts=self.accounts(actor)
        rows=[]
        if excel:
            raw=payload.get('rows')
            if not isinstance(raw,list) or not 1<=len(raw)<=200:
                raise PublishingError('invalid_rows','Excel 必须包含 1 至 200 行数据',400)
            by_x={str(a.get('x_user_id')):a for a in accounts.values()}
            for row in raw:
                if not isinstance(row,dict):
                    raise PublishingError('invalid_rows','Excel 数据行格式无效',400)
                external=row.get('x_user_id')
                account=by_x.get(external) if isinstance(external,str) else None
                try:
                    mode=processing(row.get('processing_mode'))
                except PublishingError:
                    # Preserve a row-local invalid mode for core validation;
                    # one bad Excel row must not discard other valid rows.
                    mode='invalid'
                rows.append({'row_number':row.get('row_number'),'account_id':str(account['id']) if account else '',
                    'material_id':row.get('material_id'),'caption':row.get('caption_template') or '',
                    'scheduled_at':row.get('scheduled_at_utc') or '',
                    'processing':mode})
        else:
            mids=payload.get('material_ids'); aids=payload.get('account_ids'); pairs=payload.get('pairs')
            schedules=payload.get('material_schedules') or {}
            if (not isinstance(mids,list) or not mids or not isinstance(aids,list) or not aids or not isinstance(pairs,list)
                    or not pairs or len(pairs)>200 or not isinstance(schedules,dict)
                    or any(not isinstance(x,str) for x in mids+aids) or len(set(mids))!=len(mids) or len(set(aids))!=len(aids)):
                raise PublishingError('invalid_pairs','请选择素材、候选账号并手工指定对应关系',400)
            assigned=set()
            for index,pair in enumerate(pairs,1):
                if (not isinstance(pair,dict) or pair.get('material_id') not in mids or pair.get('account_id') not in aids
                        or not isinstance(pair.get('caption_template'),str) or not pair['caption_template'].strip()):
                    raise PublishingError('invalid_pairs','每个配对必须属于已选范围，且发布文案不能为空',400)
                assigned.add(pair['material_id'])
                rows.append({'row_number':index,'account_id':pair['account_id'],'material_id':pair['material_id'],
                    'caption':pair['caption_template'],'scheduled_at':schedules.get(pair['material_id']) or '',
                    'processing':processing(payload.get('processing_mode'))})
            if set(mids)!=assigned:
                raise PublishingError('unassigned_material','存在未分配目标账号的素材',400)
        mids=list(dict.fromkeys(r['material_id'] for r in rows if isinstance(r['material_id'],str) and re.fullmatch(r'[1-9][0-9]{0,30}',r['material_id'])))
        resolved=self.source.resolve(mids) if mids else {'items':{},'errors':{}}
        result=self.store.preview(actor,{'source':'excel' if excel else 'manual','rows':rows},accounts,resolved['items'])
        return self.preview_dto(result,resolved.get('errors',{}))

    @staticmethod
    def preview_dto(result,errors=None):
        items,skipped=[],[]
        errors=errors or {}
        for row in result['rows']:
            if not row.get('valid') and not row.get('repeat_required'):
                error=errors.get(row.get('material_id'),{})
                skipped.append({'row_number':row['row_number'],'message':error.get('message') or row.get('error_message') or '该行不可发布',
                                'code':error.get('code') or row.get('error_code') or 'invalid_row'})
                continue
            material=row['source_snapshot'];account=row['account_snapshot']
            items.append({'row_number':row['row_number'],'material_id':row['material_id'],
                'material_name':material.get('material_name') or material.get('drama_name') or row['material_id'],
                'account_id':row['account_id'],'x_user_id':account.get('x_user_id',''),
                'account_name':account.get('display_name') or account.get('username') or row['account_id'],
                'caption':row['rendered_caption'],'caption_template':row['caption'],
                'planned_at_utc':row['scheduled_at'] or None,'schedule_kind':'scheduled' if row['scheduled_at'] else 'immediate',
                'processing_mode':client_processing(row['processing']),'repeat_required':bool(row.get('repeat_required'))})
        return {'preview_id':result['preview_token'],'expires_at_utc':result['expires_at'],'items':items,'skipped':skipped,
                'repeat_required':any(i['repeat_required'] for i in items)}

    def dispatch(self,method,path,actor,payload=None,query=None):
        payload=payload or {};query=query or {}
        from .gateway import route,PREFIX
        route(method,PREFIX+path)
        # Every call gets a trusted identity; scheduler and final publication recheck separately.
        actor=self.oauth.require_actor_subject(actor)
        if method=='POST' and not self.enabled and not re.fullmatch(r'/accounts/[1-9][0-9]*/pool/pause',path):
            raise PublishingError('feature_disabled','X 个人视频发布暂未启用',503)
        if method=='GET' and path=='/accounts':
            return {'items':[self.account_dto(actor,a) for a in self.accounts(actor).values()]}
        if path=='/languages':
            return {'items':[{'value':v,'label':label} for v,label in LANGUAGES]}
        if path=='/materials':
            return self.source.listing(query)
        if path=='/draft':
            if method=='POST':
                if payload.get('draft') is None:
                    self.store.delete_draft(actor)
                else:
                    self.store.save_draft(actor,{'data':payload['draft']})
            draft=self.store.get_draft(actor)
            return {'draft':draft['data'],'version':draft['version']}
        match=re.fullmatch(r'/accounts/([1-9][0-9]*)/settings',path)
        if match:
            account=self.account(actor,match[1])
            self.store.save_settings(actor,match[1],{'version':payload.get('expected_version'),'default_copy':payload.get('caption_template')},account)
            return {'account':self.account_dto(actor,account)}
        if path in {'/preflight','/excel/preview'}:
            return self.preview(actor,payload,excel=path=='/excel/preview')
        if path=='/batches' and method=='POST':
            token=payload.get('preview_id');key=payload.get('idempotency_key')
            frozen=self.store.get_preview(actor,token)
            existing=self.store.operation(actor,key)
            if existing:
                # Let core check that the exact token belongs to this idempotent operation.
                batch=self.store.commit(actor,token,key)
            else:
                rows=[r for r in frozen['rows'] if r.get('valid') or r.get('repeat_required')]
                if frozen['source']=='manual' and len(rows)!=len(frozen['rows']):
                    raise PublishingError('manual_preflight_failed','请修正所有手动发布配对后重新核对',409)
                mids=list(dict.fromkeys(r['material_id'] for r in rows))
                resolved=self.source.resolve(mids) if mids else {'items':{}}
                batch=self.store.commit(actor,token,key,self.accounts(actor),resolved['items'],allow_repeat_success=payload.get('allow_repeat') is True)
            value=batch_dto(batch)
            return {'batch_id':value['batch_id'],'batch':value}
        if method=='GET' and path=='/batches':
            try:
                limit=min(50,max(1,int(query.get('limit') or 30)));offset=max(0,int(query.get('cursor') or 0))
            except (ValueError,TypeError):
                raise PublishingError('invalid_query','分页参数无效',400) from None
            result=self.store.list_batches(actor,limit=limit,offset=offset)
            return {'items':[batch_dto(b, include_items=False) for b in result['items']],
                    'next_cursor':str(offset+limit) if offset+limit<result['total'] else ''}
        match=re.fullmatch(r'/batches/([1-9][0-9]*)',path)
        if match:
            return {'batch':batch_dto(self.store.get_batch(actor,match[1]))}
        match=re.fullmatch(r'/operations/([A-Za-z0-9._:-]{1,160})',path)
        if match:
            batch=self.store.operation(actor,match[1])
            if not batch:
                raise PublishingError('operation_not_found','尚未找到该操作，请保留原请求查询或重试',404)
            value=batch_dto(batch)
            return {'batch_id':value['batch_id'],'batch':value}
        match=re.fullmatch(r'/accounts/([1-9][0-9]*)/pool(?:/(.*))?',path)
        if match:
            aid,action=match[1],match[2] or '';self.account(actor,aid)
            pool=self._pool()
            if method=='GET':
                if action.startswith('operations/'):
                    result=pool.existing(actor,aid,action[len('operations/'):])
                    if result is None:
                        raise PublishingError('operation_not_found','尚未找到该入池操作，请保留原请求查询或重试',404)
                    return result
                return pool.listing(actor,aid,query)
            if action=='settings':return pool.settings(actor,aid,payload)
            if action in {'enable','pause'}:return pool.toggle(actor,aid,payload,action=='enable')
            if action=='items/preview':return pool.preview(actor,aid,payload)
            if action=='items':return pool.add(actor,aid,payload)
            if action in {'move','remove'}:return pool.reorder_or_remove(actor,aid,payload,action=='remove')
        raise PublishingError('not_found','接口不存在',404)
