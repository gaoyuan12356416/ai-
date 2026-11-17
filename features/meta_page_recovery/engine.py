"""One durable write chain per Ad, isolated from all other publishing owners."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime,timezone,timedelta
import json
import time
import threading
import hashlib

from .domain import (active_ad, account_dates, build_creative, creative_page, digest, encoded,
                     purchases, select_page, utcnow, UNCERTAIN)
from .meta import Graph, MetaError
from .sources import SourceError


def log(event, **values):
    print(encoded(dict(event=event,**values)),flush=True)


def scan(store, sources):
    run=store.new_run()
    errors=[]
    try:
        pages=sources.pages()
        baseline=store.observe(run,pages)
        if not baseline:
            store.schedule_retries(run)
            for incident in store.incidents():
                try:
                    cursor=incident['cursor']
                    while True:
                        rows,new_cursor,complete=sources.ads(incident['page_id'],cursor)
                        if not complete and new_cursor==cursor:raise SourceError('ad_pagination_not_advancing')
                        store.add_jobs(run,incident,rows,new_cursor,complete)
                        cursor=new_cursor
                        if complete:break
                except (SourceError,ValueError) as exc:
                    errors.append('discovery:'+incident['id']+':'+str(exc))
    except (SourceError,ValueError,KeyError) as exc:
        errors.append('scan:'+type(exc).__name__+':'+str(exc))
    except Exception as exc:
        errors.append('scan_internal:'+type(exc).__name__)
    store.scan_done(run,';'.join(errors) or None)
    store.close_ready_runs()
    log('scan_finished',run_id=run,errors=errors)
    return run


def contains(expected,actual):
    """Meta may add derived fields, but may not silently remove original content."""
    if isinstance(expected,dict):
        return isinstance(actual,dict) and all(k in actual and contains(v,actual[k]) for k,v in expected.items())
    if isinstance(expected,list):
        return isinstance(actual,list) and len(expected)==len(actual) and all(contains(a,b) for a,b in zip(expected,actual))
    return expected==actual or (str(expected)==str(actual) and not isinstance(expected,(dict,list)))


def verify_creative(creative,payload):
    expected={k:v for k,v in payload.items() if k!='name'}
    actual=dict(creative)
    spec=dict(actual.get('object_story_spec') or {})
    if spec.get('instagram_actor_id') and not spec.get('instagram_user_id'):
        spec['instagram_user_id']=spec['instagram_actor_id']
    actual['object_story_spec']=spec
    if not contains(expected,actual):raise ValueError('creative_content_readback_mismatch')


class Worker:
    def __init__(self,store,sources,env,profile_loader,graph_factory=None,stop=None):
        self.store,self.sources,self.env,self.profile_loader=store,sources,env,profile_loader
        self.graph_factory=graph_factory or (lambda token:Graph(token,env.get('META_PAGE_RECOVERY_GRAPH_VERSION','v25.0')))
        self.allocations=Counter()
        self.graphs={}
        self.stop=stop or threading.Event()
        self.pool_snapshot=None
        self.profile_snapshot=None
        self.auth_graphs={}
        self.auth_pages={}
        self.auth_locks={}

    def graph(self,data):
        key=str(data['ad_account_id'])
        if key in self.graphs:return self.graphs[key]
        for token in self.sources.tokens(data):
            graph=self.graph_factory(token)
            try:
                ad=graph.ad(data['ad_id'])
                if str(ad.get('account_id'))!=key.removeprefix('act_'):continue
                self.graphs[key]=graph
                return graph
            except MetaError as exc:
                if exc.transient:raise
        raise MetaError('no_authorized_token')

    def proof_graph(self,data,proof):
        fingerprint=proof.get('credential_fingerprint')
        if fingerprint:
            for token in self.sources.tokens(data):
                if hashlib.sha256(token.encode()).hexdigest()==fingerprint:
                    return self.graph_factory(token)
        return self.graph(data)

    def write(self,job,graph,stage,path,payload,files=None,record=None):
        attempt=self.store.intent(job['id'],stage,record if record is not None else payload)
        try:
            receipt=graph.request('POST',path,payload,files=files) if files else graph.post(path,**payload)
        except MetaError as exc:
            self.store.receipt(attempt,'unknown' if exc.uncertain else 'rejected',error=str(exc))
            raise
        except Exception:
            self.store.receipt(attempt,'unknown',error='transport_internal')
            raise MetaError('transport_internal',uncertain=True) from None
        self.store.receipt(attempt,'accepted',receipt)
        return receipt

    def authorized_selection(self,data,pages,profiles,target,excluded,current,proof=None):
        if not hasattr(current,'account_pages'):
            return current,select_page(pages,profiles,target,data['group_id'],self.allocations,excluded)
        account=str(data['ad_account_id'])
        tokens=self.sources.tokens(data)
        account_key=(account,tuple(hashlib.sha256(token.encode()).hexdigest() for token in tokens))
        if account_key not in self.auth_graphs:
            graphs=[]
            for token in tokens:
                graph=self.graph_factory(token)
                try:
                    ad=graph.ad(data['ad_id'])
                    if str(ad.get('account_id'))==account.removeprefix('act_'):graphs.append(graph)
                except MetaError:continue
            self.auth_graphs[account_key]=graphs
        unknown=[];owners={}
        for graph in self.auth_graphs[account_key]:
            key=hashlib.sha256(graph.token.encode()).hexdigest()
            lock=self.auth_locks.setdefault(key,threading.Lock())
            with lock:
                if key not in self.auth_pages:self.auth_pages[key]=graph.account_pages()
                allowed,complete=self.auth_pages[key]
            for pid in allowed:owners.setdefault(pid,[]).append(graph)
            if not complete:unknown.append(graph)
        if proof is not None:
            signature=digest({pid:[hashlib.sha256(g.token.encode()).hexdigest() for g in graphs] for pid,graphs in owners.items()})
            if proof.get('authorization_scope_signature')!=signature:
                excluded=set(excluded)-set(proof.get('permission_rejected_pages',[]))
                excluded.add(data['origin_page_id'])
                proof['permission_rejected_pages']=[]
                proof['permission_rejected_assignments']=[]
                proof['authorization_scope_signature']=signature
        rejected=set((proof or {}).get('permission_rejected_assignments',[]))
        assignment=lambda graph,pid:hashlib.sha256(graph.token.encode()).hexdigest()+':'+pid
        owners={pid:[graph for graph in graphs if assignment(graph,pid) not in rejected] for pid,graphs in owners.items()}
        known=[p for p in pages if owners.get(str(p['page_id']))]
        selected=select_page(known,profiles,target,data['group_id'],self.allocations,excluded)
        if selected:return owners[selected['page_id']][0],selected
        # Missing metadata permission or bounded/incomplete enumeration is not
        # negative authorization evidence. Fall back to actual Meta validation.
        for graph in unknown:
            eligible=[p for p in pages if assignment(graph,str(p['page_id'])) not in rejected]
            selected=select_page(eligible,profiles,target,data['group_id'],self.allocations,excluded)
            if selected:return graph,selected
        return current,None

    def reupload(self,job,graph,payload,proof):
        from .domain import video_ids,replace_video
        from .media import copy_video
        proof['media_reupload_attempted']=True
        proof['media_original_payload']=payload
        proof.setdefault('media_maps',{})
        proof.setdefault('unused_creatives',[])
        if proof.get('new_creative_id'):proof['unused_creatives'].append(proof.pop('new_creative_id'))
        self.store.save_proof(job['id'],proof)
        for vid in video_ids(payload):
            new=copy_video(graph,job['payload']['ad_account_id'],vid,
                self.store.path.parent/'media'/job['id'],
                lambda stage,path,params,files=None,record=None:self.write(job,graph,stage,path,params,files,record))
            payload=replace_video(payload,vid,new)
            proof['media_maps'][vid]=new
            self.store.save_proof(job['id'],proof)
        payload['name']='mpr-'+job['id']+'-media'
        proof['creative_payload']=payload
        self.store.save_proof(job['id'],proof)
        return payload

    def resume_proof(self,job):
        proof=job['proof']
        for attempt in self.store.attempts(job['id']):
            if attempt['stage']=='creative_create' and attempt['status']=='accepted':
                receipt=json.loads(attempt['receipt'])
                if receipt.get('id') and str(receipt['id']) not in proof.get('unused_creatives',[]):
                    proof.update(new_creative_id=str(receipt['id']),creative_payload=json.loads(attempt['request']))
            if attempt['stage']=='ad_update' and attempt['status']=='accepted':
                proof['ad_update_accepted']=True
        return proof

    def process(self,job):
        data=job['payload'];proof=self.resume_proof(job);stage='prepare'
        try:
            pages=self.pool_snapshot if self.pool_snapshot is not None else self.sources.pages()
            origins=[p for p in pages if str(p['page_id'])==data['origin_page_id'] and p['group_id']==data['group_id']]
            if len(origins)!=1:
                self.store.result(job['id'],'skipped','origin_pool_membership_changed',proof);return
            from .domain import bad
            if not bad(origins[0]):
                self.store.result(job['id'],'skipped','source_page_recovered',proof);return
            graph=self.proof_graph(data,proof)
            current=graph.ad(data['ad_id'])
            if str(current.get('id'))!=str(data['ad_id']):raise ValueError('ad_identity_mismatch')
            if str(current.get('account_id'))!=str(data['ad_account_id']).removeprefix('act_'):
                raise ValueError('ad_account_identity_mismatch')
            current_cid=str((current.get('creative') or {}).get('id') or '')
            if proof.get('new_creative_id')==current_cid:
                return self.confirm(job,graph,current,proof)
            creative=graph.creative(current_cid)
            if creative_page(creative)!=data['origin_page_id']:
                self.store.result(job['id'],'superseded','current_page_already_changed',proof);return
            if proof.get('old_creative_id') and current_cid!=proof['old_creative_id']:
                self.store.result(job['id'],'superseded','creative_changed_externally',proof);return
            if not active_ad(current):
                self.store.result(job['id'],'skipped','ad_or_parent_not_active',proof);return
            account=graph.get(str(data['ad_account_id']),fields='id,timezone_name')
            dates=account_dates(account)
            rows=graph.insights(data['ad_id'],dates)
            if any(str(r.get('ad_id'))!=str(data['ad_id']) for r in rows):raise ValueError('pur_ad_identity_mismatch')
            pur=purchases(rows)
            proof.update(pur=pur,pur_dates=dates,timezone_name=account['timezone_name'],old_creative_id=current_cid,
                         original_creative=creative,original_ad=current)
            self.store.save_proof(job['id'],proof)
            if pur is None:
                self.store.result(job['id'],'retry_wait','pur_no_data',proof);return
            if Decimal(pur)<=0:
                self.store.result(job['id'],'skipped','pur_zero',proof);return
            if not proof.get('new_creative_id'):
                target=(self.sources.target(data,origins[0]) if str(data.get('creative_id'))==current_cid
                        else dict(language=origins[0].get('language'),audience='__unknown__',themes=[],classification_status='cached_creative_stale'))
                if self.profile_snapshot is None:self.profile_snapshot=self.profile_loader([str(p['page_id']) for p in pages])
                profiles,metadata,warnings=self.profile_snapshot
                excluded={data['origin_page_id']}
                excluded.update(proof.get('permission_rejected_pages',[]))
                # A Page can be unavailable for one account and usable for
                # others. These exclusions never mutate the source Page pool.
                probes=0
                while True:
                    # A later positive Page grant clears an earlier local
                    # rejection. An unrelated account/Page keeps progressing.
                    graph,selected=self.authorized_selection(data,pages,profiles,target,excluded,graph,proof)
                    excluded={data['origin_page_id']}|set(proof.get('permission_rejected_pages',[]))
                    if selected is None:
                        proof['permission_rejected_pages']=[]
                        proof['permission_rejected_assignments']=[]
                        self.store.result(job['id'],'waiting_no_candidate','no_authorized_same_pool_page',proof);return
                    selected_id=selected['page_id']
                    proof.update(new_page_id=selected_id,selection=selected,target=target,profile_version=metadata,
                                 profile_warnings=warnings)
                    if hasattr(graph,'token'):proof['credential_fingerprint']=hashlib.sha256(graph.token.encode()).hexdigest()
                    self.store.save_proof(job['id'],proof)
                    try:
                        instagram=graph.new_instagram(selected_id)
                        try:payload=build_creative(creative,selected_id,job['id'],instagram)
                        except ValueError:
                            creative=graph.story_content(creative)
                            payload=build_creative(creative,selected_id,job['id'],instagram)
                        if proof.get('media_maps'):
                            from .domain import replace_video
                            for old,new in proof['media_maps'].items():payload=replace_video(payload,old,new)
                            payload['name']='mpr-'+job['id']+'-media'
                        stage='creative_create'
                        receipt=self.write(job,graph,stage,str(data['ad_account_id'])+'/adcreatives',payload)
                        cid=str(receipt.get('id') or '')
                        if not cid:raise MetaError('creative_receipt_missing',uncertain=True)
                        proof.update(new_creative_id=cid,creative_payload=payload)
                        self.store.save_proof(job['id'],proof)
                        break
                    except MetaError as exc:
                        if exc.uncertain:raise
                        if exc.code in {'page_unpublished',200} or exc.subcode in {1487194,1443017,2490604,3858749}:
                            if hasattr(graph,'token'):
                                proof.setdefault('permission_rejected_assignments',[]).append(hashlib.sha256(graph.token.encode()).hexdigest()+':'+selected_id)
                            else:
                                excluded.add(selected_id)
                                proof.setdefault('permission_rejected_pages',[]).append(selected_id)
                            self.store.save_proof(job['id'],proof)
                            probes+=1
                            if probes>=3:
                                self.store.result(job['id'],'waiting_no_candidate','page_permission_probes_deferred',proof);return
                            stage='prepare';continue
                        from .domain import video_ids
                        if exc.code==100 and video_ids(payload) and not proof.get('media_reupload_attempted'):
                            stage='media_upload'
                            payload=self.reupload(job,graph,payload,proof)
                            stage='creative_create'
                            receipt=self.write(job,graph,stage,str(data['ad_account_id'])+'/adcreatives',payload)
                            if not receipt.get('id'):raise MetaError('creative_receipt_missing',uncertain=True)
                            proof.update(new_creative_id=str(receipt['id']),creative_payload=payload)
                            self.store.save_proof(job['id'],proof)
                            break
                        raise
                self.allocations[selected_id]+=1
            stage='validate'
            created=graph.creative(proof['new_creative_id'])
            if str(created.get('id'))!=proof['new_creative_id']:raise ValueError('creative_identity_mismatch')
            verify_creative(created,proof['creative_payload'])
            # validate_only never changes the Ad; an interrupted validation
            # can be repeated without replaying a live mutation.
            try:
                graph.post(data['ad_id'],creative={'creative_id':proof['new_creative_id']},execution_options=['validate_only'])
            except MetaError as exc:
                from .domain import video_ids
                if exc.code!=100 or exc.uncertain or proof.get('media_reupload_attempted') or not video_ids(proof['creative_payload']):raise
                stage='media_upload'
                payload=self.reupload(job,graph,proof['creative_payload'],proof)
                stage='creative_create'
                receipt=self.write(job,graph,stage,str(data['ad_account_id'])+'/adcreatives',payload)
                if not receipt.get('id'):raise MetaError('creative_receipt_missing',uncertain=True)
                proof.update(new_creative_id=str(receipt['id']),creative_payload=payload)
                self.store.save_proof(job['id'],proof)
                verify_creative(graph.creative(proof['new_creative_id']),payload)
                stage='validate'
                graph.post(data['ad_id'],creative={'creative_id':proof['new_creative_id']},execution_options=['validate_only'])
            # Re-read both source membership and target status after preparation.
            fresh=(self.sources.page_status([data['origin_page_id'],proof['new_page_id']])
                   if hasattr(self.sources,'page_status') else self.sources.pages())
            from .domain import available,bad
            if not any(p['group_id']==data['group_id'] and str(p['page_id'])==data['origin_page_id'] and bad(p) for p in fresh):
                self.store.result(job['id'],'skipped','source_recovered_or_left_pool_during_prepare',proof);return
            if not any(p['group_id']==data['group_id'] and str(p['page_id'])==proof['new_page_id'] and available(p) for p in fresh):
                proof.setdefault('unused_creatives',[]).append(proof.pop('new_creative_id'))
                self.store.result(job['id'],'retry_wait','selected_page_changed_before_update',proof);return
            latest=graph.ad(data['ad_id'])
            if not active_ad(latest):
                self.store.result(job['id'],'skipped','ad_or_parent_paused_during_prepare',proof);return
            if str((latest.get('creative') or {}).get('id'))!=proof['old_creative_id']:
                self.store.result(job['id'],'superseded','creative_changed_during_prepare',proof);return
            stage='ad_update'
            receipt=self.write(job,graph,stage,data['ad_id'],{'creative':{'creative_id':proof['new_creative_id']}})
            if receipt.get('success') is not True:raise MetaError('ad_receipt_missing',uncertain=True)
            proof.update(ad_update_accepted=True,swapped_at=utcnow())
            self.store.save_proof(job['id'],proof)
            self.confirm(job,graph,graph.ad(data['ad_id']),proof)
        except MetaError as exc:
            if stage=='ad_update' and (exc.uncertain or proof.get('ad_update_accepted')):status='unknown_ad'
            elif stage=='creative_create' and exc.uncertain:status='unknown_creative'
            elif stage=='media_upload' and exc.uncertain:status='unknown_media'
            elif stage=='validate' and exc.uncertain:status='retry_wait'
            elif exc.transient or exc.code in {190,'no_authorized_token','transport'}:status='retry_wait'
            else:status='failed'
            self.store.result(job['id'],status,str(exc),proof)
            if exc.transient:time.sleep(2)
        except (SourceError,ValueError,KeyError) as exc:
            self.store.result(job['id'],'unknown_ad' if proof.get('ad_update_accepted') else ('retry_wait' if isinstance(exc,SourceError) else 'failed'),str(exc)[:160],proof)
        except Exception as exc:
            # An unexpected failure after any unreceipted intent stays unknown.
            intents=[a for a in self.store.attempts(job['id']) if a['status'] in {'intent','unknown'}]
            status=('unknown_ad' if intents[-1]['stage']=='ad_update' else 'unknown_creative') if intents else 'failed'
            self.store.result(job['id'],status,'internal_'+type(exc).__name__,proof)
        finally:
            log('job_finished',job_id=job['id'],ad_id=data['ad_id'])

    def confirm(self,job,graph,ad,proof):
        if str(ad.get('id'))!=str(job['payload']['ad_id']):raise ValueError('ad_identity_mismatch')
        cid=str((ad.get('creative') or {}).get('id') or '')
        if cid!=proof.get('new_creative_id'):
            self.store.result(job['id'],'unknown_ad','ad_reference_not_confirmed',proof);return
        created=graph.creative(cid)
        verify_creative(created,proof['creative_payload'])
        if creative_page(created)!=proof.get('new_page_id'):raise ValueError('page_readback_mismatch')
        effective=ad.get('effective_status')
        proof.update(confirmed_at=utcnow(),effective_status=effective)
        state='deliverable' if effective=='ACTIVE' else 'pending_review' if effective=='PENDING_REVIEW' else 'changed'
        self.store.result(job['id'],state,'meta_effective_status_'+str(effective) if state=='changed' else '',proof)

    def reconcile(self):
        for job in self.store.jobs(UNCERTAIN|{'pending_review','changed'}):
            if datetime.fromisoformat(job['updated'])>datetime.now(timezone.utc)-timedelta(hours=1):continue
            try:
                proof=self.resume_proof(job);graph=self.proof_graph(job['payload'],proof)
                ad=graph.ad(job['payload']['ad_id'])
                cid=str((ad.get('creative') or {}).get('id') or '')
                if proof.get('new_creative_id')==cid:
                    self.confirm(job,graph,ad,proof)
                elif job['status']=='unknown_creative':
                    # Only a full, exact operation-name + payload match can
                    # recover a lost create receipt. Absence is not rejection.
                    attempts=[a for a in self.store.attempts(job['id']) if a['stage']=='creative_create' and a['status'] in {'unknown','intent'}]
                    if not attempts:continue
                    expected=json.loads(attempts[-1]['request'])
                    found=graph.pages(str(job['payload']['ad_account_id'])+'/adcreatives',fields='id,name',limit=100,max_pages=3)
                    matches=[r for r in found if r.get('name')==expected['name']]
                    if len(matches)==1:
                        created=graph.creative(matches[0]['id']);verify_creative(created,expected)
                        proof.update(new_creative_id=str(matches[0]['id']),creative_payload=expected)
                        self.store.receipt(attempts[-1]['id'],'accepted',{'id':str(matches[0]['id']),'reconciled':True})
                        self.store.result(job['id'],'retry_wait','creative_receipt_reconciled',proof)
                elif job['status']=='unknown_media':
                    from .domain import video_ids,replace_video
                    original=proof.get('media_original_payload')
                    if not original:continue
                    maps=dict(proof.get('media_maps') or {})
                    attempts=self.store.attempts(job['id'])
                    for start in attempts:
                        if start['stage']!='video_start' or start['status']!='accepted':continue
                        request=json.loads(start['request']);receipt=json.loads(start['receipt']);vid=str(receipt.get('video_id') or '')
                        finish=[a for a in attempts if a['stage']=='video_finish' and json.loads(a['request']).get('video_id')==vid]
                        if not finish:continue
                        # Ready plus the exact receipted source fingerprint is
                        # affirmative evidence; processing timeout alone is not.
                        record=json.loads(finish[-1]['request'])
                        if record.get('sha256')!=request.get('sha256') or record.get('file_size')!=request.get('file_size'):continue
                        status=graph.get(vid,fields='id,status').get('status') or {}
                        if status.get('video_status')=='ready':maps[str(request['original_video_id'])]=vid
                    if all(vid in maps for vid in video_ids(original)):
                        payload=original
                        for old,new in maps.items():payload=replace_video(payload,old,new)
                        proof.update(media_maps=maps,creative_payload=payload)
                        self.store.result(job['id'],'retry_wait','uploaded_media_reconciled',proof)
                elif job['status'] in {'pending_review','changed'}:
                    self.store.result(job['id'],'superseded','creative_changed_after_swap',proof)
            except Exception as exc:
                log('reconciliation_deferred',job_id=job['id'],error_type=type(exc).__name__)
            finally:
                self.store.touch(job['id'])

    def account(self,account):
        # One attempt per queued job; retries are scheduled by the next scan,
        # so an unavailable account cannot spin or monopolize this worker.
        while not self.stop.is_set():
            job=self.store.claim(account)
            if not job:return
            self.process(job)

    def tick(self):
        accounts=list(dict.fromkeys(j['account_id'] for j in self.store.jobs({'queued'})))
        if accounts:self.pool_snapshot=self.sources.pages()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.account,accounts))
        self.reconcile()
        self.store.close_ready_runs()
