"""Bounded network workers; FIFO SQL and both durable ledgers stay on owner thread.

Every phase drains before returning. A worker never accesses SQLite or SQL.
Only a committed object claim may enter a write worker. Unknown writes are
reconciled by GET and never submitted a second time.
"""
from collections import defaultdict,deque
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
import threading
import time

from .current_contexts import current_contexts
from .graph import GraphClient
from .throttle import Governor,GovernedSession,RequestNotSent
from .runtime import legacy_delete_lock
from .runner import safe_error,ledger_error


def waves(items,size,per_account):
    accounts=defaultdict(deque)
    for item in items:accounts[item['account_id']].append(item)
    order=deque(accounts)
    while order:
        batch=[];selected=defaultdict(int);rounds=len(order)*per_account
        width=size() if callable(size) else size
        if type(width) is not int or width<1:raise ValueError('invalid_execution_wave_size')
        for _ in range(rounds):
            if not order or len(batch)>=width:break
            account=order.popleft()
            if selected[account]<per_account:
                batch.append(accounts[account].popleft());selected[account]+=1
            if accounts[account]:order.append(account)
        yield batch


class ConcurrentExecutor:
    def __init__(self,runner,run_id,manifest,job_id,shared_run,entries,groups,record,counts):
        self.runner=runner;self.run_id=run_id;self.manifest=manifest;self.job=job_id;self.shared=shared_run
        self.entries=entries;self.groups=groups;self.record=record;self.counts=counts
        self.maximum=runner.config['execution_workers'];self.per_account=runner.config.get('execution_per_account',2)
        self.batch_size=runner.config.get('execution_batch_size',32)
        self.ttl=runner.config.get('execution_context_ttl_seconds',30)
        self.governor=Governor(self.maximum,self.per_account,runner.config.get('execution_max_rps',24))
        self.abort=threading.Event();self.local=threading.local();self.graphs=[];self.graph_lock=threading.Lock()
        self.pool=ThreadPoolExecutor(max_workers=self.maximum)
        self.timings=defaultdict(float);self.completed=0;self.started=time.monotonic()
        self.safe_retries=defaultdict(int)
        self.confirmed_basis={}
        # Shared-reference writes use the same budget after the parallel phases.
        if isinstance(runner.execution_graph,GraphClient):
            runner.execution_graph.http=GovernedSession(runner.execution_graph.http,self.governor,self.stopped)

    def stopped(self):return self.abort.is_set() or self.runner.stop()

    def graph(self,account,write_check=lambda:None):
        if not isinstance(self.runner.graph,GraphClient):return self.runner.graph
        graph=getattr(self.local,'graph',None)
        if graph is None:
            graph=GraphClient(self.runner.graph.tokens,self.runner.graph.version,
                timeout=min(20,self.runner.graph.timeout),get_retries=0)
            graph.share_read_metrics(self.runner.graph);graph.stop=self.stopped;graph.delete_stop=self.stopped
            graph.http=GovernedSession(graph.http,self.governor,self.stopped)
            with self.graph_lock:self.graphs.append(graph)
            self.local.graph=graph
        graph.http.account=account;graph.http.write_check=write_check
        return graph

    def close(self):
        self.abort.set();self.pool.shutdown(wait=True)
        for graph in self.graphs:graph.close()

    def contexts(self,batch):
        source=self.runner.source
        if hasattr(source,'_session'):return current_contexts(source,batch)
        # Test/custom adapters retain the same no-worker-SQL contract.
        with self.runner.sql_snapshot():
            return {(c['ad_id'],c['creative_id']):source.current_ad_context(c['ad_id'],c['creative_id']) for c in batch}

    def preflight(self,kind,candidate,context,receipt):
        from .phased_execution import basis_valid
        graph=self.graph(candidate['account_id'])
        if self.stopped():raise InterruptedError('stop_requested')
        if context.get('error'):return {'eligible':False,'reason':context['error']}
        if kind=='ad':
            # This checks SQL identity only against the frozen eligible record.
            # The actual current Meta record is fully re-evaluated by the one
            # final identity GET inside Graph.delete, immediately before DELETE.
            return self.runner.preflight(candidate,self.manifest,context=context,ad=candidate['ad_snapshot'])
        entry=self.entries[candidate['creative_id']]
        valid,reason=basis_valid(self.runner,candidate,entry['evidence'],entry.get('disabled_account',False),
            context=context,graph=graph,receipt=receipt)
        return {'eligible':valid,'reason':reason}

    def write(self,kind,candidate,lease,checked_at,sql_context):
        identifier=candidate['ad_id'] if kind=='ad' else candidate['creative_id']
        def check():
            if self.stopped():raise RequestNotSent('cancelled_before_request')
            if time.monotonic()-checked_at>self.ttl:raise RequestNotSent('execution_context_expired')
            try:
                if lease is not None:lease.check()
            except InterruptedError:raise RequestNotSent('legacy_priority_before_request') from None
        context={'credential_kind':'user','credential_user_id':candidate['user_id'],
            'credential_relation':'frozen_publish_queue','account_id':candidate['account_id']}
        graph=self.graph(candidate['account_id'],check);result=None
        try:
            check()
            guard=lambda ad:self.runner.preflight(candidate,self.manifest,context=sql_context,ad=ad)
            kwargs={'expected_creative_id':candidate['creative_id'],'ad_guard':guard} if kind=='ad' and isinstance(graph,GraphClient) else {}
            if kind=='ad' and not isinstance(graph,GraphClient):
                decision=guard(graph.get_ad(identifier,candidate['user_id']))
                if not decision['eligible']:return dict(context,status='failed',code='current_policy_changed',request_sent=False,decision=decision)
            result=graph.delete(kind,identifier,candidate['user_id'],candidate['account_id'],**kwargs)
            result=dict(result,**context,delete_status=result['status'],api_accepted=result['status']=='api_accepted')
            if result['status'] in ('api_accepted','unknown'):
                # Stop prevents new DELETEs, but drain the readback of writes
                # already sent. Keep this GET bounded even during a long cooldown.
                if isinstance(graph,GraphClient):
                    previous_stop=graph.stop;previous_http_stop=graph.http.stop
                    verification_deadline=time.monotonic()+20
                    graph.stop=graph.http.stop=lambda:time.monotonic()>verification_deadline
                    try:verification=graph.reconcile(kind,identifier,candidate['user_id'],candidate['account_id'])
                    finally:graph.stop=previous_stop;graph.http.stop=previous_http_stop
                else:verification=graph.reconcile(kind,identifier,candidate['user_id'],candidate['account_id'])
                result['verification']=verification
                if verification.get('status')=='deleted_verified':result.update(status='deleted_verified',confirmed_deleted=True)
                else:result['status']='unknown'
            return result
        except RequestNotSent as exc:
            if result is None:return dict(context,status='failed',request_sent=False,code=exc.code,api_accepted=False)
            return dict(context,status='unknown',prior_result=result,api_accepted=result.get('api_accepted',False),requires_reconciliation=True)
        except BaseException as exc:
            return dict(context,status='unknown',error=safe_error(exc),prior_result=result,
                api_accepted=bool(result and result.get('api_accepted')),requires_reconciliation=True)

    def claim(self,kind,candidate):
        identifier=candidate['ad_id'] if kind=='ad' else candidate['creative_id'];key=kind+':'+identifier
        self.runner.check_stop()
        claim=self.runner.store.claim_object(self.job,key,self.shared)
        if not claim.get('claimed'):
            return {'status':claim['status'],'reason':claim.get('reason','existing_receipt_or_fence'),
                'request_sent':False,'prior_result':claim.get('result',{})}
        self.runner.store.record_object_credential(self.job,key,self.shared,{
            'credential_kind':'user','credential_user_id':candidate['user_id'],'credential_relation':'frozen_publish_queue'})
        self.runner.runtime.write_barrier(self.run_id)
        return None

    def persist(self,kind,candidate,result):
        identifier=candidate['ad_id'] if kind=='ad' else candidate['creative_id']
        status='deleted' if result['status']=='deleted_verified' else 'failed' if result['status']=='failed' else 'unknown'
        self.runner.store.finish_object(self.job,kind+':'+identifier,self.shared,status,result)

    def phase(self,kind,items):
        from .phased_execution import object_state,has_ad_receipt
        # Keep startup/cooldown waves small; widen only with observed headroom.
        for batch in waves(items,lambda:min(self.batch_size,self.governor.limit*4),self.per_account):
            self.runner.check_stop();self.governor.wait_ready(self.stopped)
            with legacy_delete_lock(self.runner.config['legacy_lock'],self.runner.stop) as lease:
                checked_at=time.monotonic()
                try:contexts=self.contexts(batch)
                except Exception as exc:
                    if ledger_error(exc) or isinstance(exc,InterruptedError):raise
                    contexts={(c['ad_id'],c['creative_id']):{'error':safe_error(exc).get('code','context_unavailable')} for c in batch}
                self.timings['sql_context_seconds']+=time.monotonic()-checked_at
                pending={};wave_started=time.monotonic();drain_error=None;retry=[]
                try:
                    for c in batch:
                        if kind=='creative' and any(object_state(self.runner.store,self.job,'ad:'+a['ad_id'])['status']
                                in ('unknown','in_progress') for a in self.groups.get(c['creative_id'],[])):
                            yield c,{'status':'deferred','request_sent':False,'reason':'ad_result_unknown'};continue
                        receipt=has_ad_receipt(self.runner.store,c['ad_id']) if kind=='creative' else None
                        future=self.pool.submit(self.preflight,kind,c,contexts[(c['ad_id'],c['creative_id'])],receipt)
                        pending[future]=('preflight',c)
                    while pending:
                        done,_=wait(pending,return_when=FIRST_COMPLETED)
                        for future in done:
                            phase,c=pending.pop(future)
                            if phase=='write':
                                result=future.result();self.persist(kind,c,result);self.completed+=1
                                if kind=='ad' and result['status']=='deleted_verified':
                                    self.confirmed_basis[c['creative_id']]=c
                                    self.save_basis(c)
                                if result.get('request_sent') is False and result.get('code') in ('execution_context_expired','legacy_priority_before_request'):
                                    key=(kind,c['ad_id'],c['creative_id'])
                                    if self.safe_retries[key]<2:self.safe_retries[key]+=1;retry.append(c)
                                yield c,result;continue
                            try:decision=future.result()
                            except Exception as exc:
                                if ledger_error(exc) or isinstance(exc,InterruptedError):raise
                                decision={'eligible':False,'reason':'preflight_failed','error':safe_error(exc)}
                            self.runner.runtime.event(self.run_id,'preflight',{'ad_id':c['ad_id'],'stage':kind,'decision':decision})
                            if not decision['eligible']:
                                if kind=='ad':self.counts['skipped_ads']+=1
                                else:yield c,{'status':'deferred','request_sent':False,'reason':decision['reason']}
                                continue
                            self.runner.check_stop()
                            if lease is not None:lease.check()
                            entry=self.entries[c['creative_id']]
                            if kind=='ad':
                                self.save_basis(self.confirmed_basis.get(c['creative_id'],c))
                            else:self.runner.runtime.queue_creative(entry)
                            prior=self.claim(kind,c)
                            if prior is not None:yield c,prior;continue
                            future=self.pool.submit(self.write,kind,c,lease,checked_at,contexts[(c['ad_id'],c['creative_id'])])
                            pending[future]=('write',c)
                except BaseException as exc:
                    self.abort.set();drain_error=exc
                finally:
                    # Never release the legacy lock or change phase while a
                    # network write is running, even if one ledger operation fails.
                    for future,(phase,c) in list(pending.items()):
                        if phase!='write':future.cancel();continue
                        result=future.result()
                        if drain_error is None or not ledger_error(drain_error):
                            try:self.persist(kind,c,result);self.record(kind,c['ad_id'] if kind=='ad' else c['creative_id'],result,kind+'s')
                            except BaseException as exc:self.abort.set();drain_error=exc
                    # Running read-only preflights must also leave the phase.
                    if pending:wait(pending)
                if drain_error:
                    if isinstance(drain_error,InterruptedError) and str(drain_error)=='legacy_priority_window' and not self.runner.stop():
                        self.abort.clear();retry=batch
                    else:raise drain_error
                self.timings['network_wave_seconds']+=time.monotonic()-wave_started
                self.runner.runtime.update(self.run_id,execution_metrics=dict(self.timings,completed_attempts=self.completed,
                    elapsed_seconds=round(time.monotonic()-self.started,3),**self.governor.snapshot()),
                    graph_metrics=getattr(self.runner.graph,'safe_metrics',{}),sql_metrics=getattr(self.runner.source,'safe_metrics',{}))
            # Refresh SQL only after releasing/waiting for the legacy lock.
            if retry:yield from self.phase(kind,retry)

    def save_basis(self,candidate):
        entry=self.entries[candidate['creative_id']]
        entry['basis']=candidate;entry['evidence']={'materials':{mid:self.manifest['evidence']['materials'][mid]
            for mid in candidate['material_ids'] if mid in self.manifest['evidence']['materials']}}
        self.runner.runtime.queue_creative(entry)
