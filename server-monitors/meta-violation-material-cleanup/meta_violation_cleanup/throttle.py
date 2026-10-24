"""Shared adaptive request budget for execution GETs and DELETEs."""
from collections import Counter
from contextlib import contextmanager
import json
import math
import threading
import time
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime


class RequestNotSent(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)


class Governor:
    def __init__(self,maximum=16,per_account=2,rps=24,clock=time.monotonic):
        self.maximum=maximum;self.limit=min(4,maximum);self.per_account=per_account;self.rps=rps
        self.clock=clock;self.lock=threading.Lock();self.active=0;self.accounts=Counter()
        self.global_until=0.;self.account_until={};self.next_request=0.;self.last_change=clock()
        self.observed=False;self.usage={};self.metrics=Counter();self.max_seen=0

    def stopped_wait(self,stop,delay=.1):
        if stop():raise RequestNotSent('cancelled_before_request')
        time.sleep(min(.25,max(.001,delay)))

    def wait_ready(self,stop):
        while True:
            with self.lock:delay=self.global_until-self.clock()
            if delay<=0:return
            self.stopped_wait(stop,delay)

    @contextmanager
    def slot(self,account,stop):
        started=self.clock()
        while True:
            if stop():raise RequestNotSent('cancelled_before_request')
            with self.lock:
                now=self.clock()
                ready=(now>=max(self.global_until,self.account_until.get(account,0),self.next_request)
                    and self.active<self.limit and self.accounts[account]<self.per_account)
                if ready:
                    self.active+=1;self.accounts[account]+=1
                    self.next_request=now+1/self.rps;self.max_seen=max(self.max_seen,self.active)
                    self.metrics['request_wait_milliseconds']+=round((now-started)*1000)
                    break
                deadline=max(self.global_until,self.account_until.get(account,0),self.next_request)
                delay=deadline-now if deadline>now else .01
            self.stopped_wait(stop,delay)
        try:yield
        finally:
            with self.lock:self.active-=1;self.accounts[account]-=1

    def observe(self,response,account):
        headers={str(k).lower():v for k,v in (getattr(response,'headers',{}) or {}).items()}
        values={};regain=0.
        for name in ('x-app-usage','x-ad-account-usage','x-business-use-case-usage'):
            raw=headers.get(name)
            if not isinstance(raw,str) or len(raw)>65536:continue
            try:nodes=[(json.loads(raw),0)]
            except (ValueError,TypeError):continue
            for _ in range(2048):
                if not nodes:break
                node,depth=nodes.pop()
                if isinstance(node,dict):
                    for key,value in node.items():
                        if type(value) in (int,float) and math.isfinite(value) and value>=0:
                            if key in ('call_count','total_cputime','total_time','acc_id_util_pct'):
                                values[name]=max(values.get(name,0),value)
                            if key=='estimated_time_to_regain_access':regain=max(regain,value*60)
                        elif isinstance(value,(list,dict)) and depth<8:nodes.append((value,depth+1))
                elif isinstance(node,list) and depth<8:nodes.extend((v,depth+1) for v in node if isinstance(v,(list,dict)))
        try:data=response.json()
        except (ValueError,TypeError):data=None
        error=data.get('error',{}) if isinstance(data,dict) else {}
        code=error.get('code') if isinstance(error,dict) else None
        limited=response.status_code==429 or code in (4,17,32,613,80000,80001,80002,80003,80004,80005,80006)
        try:retry=max(0.,float(headers.get('retry-after',0)))
        except (ValueError,TypeError):
            try:
                until=parsedate_to_datetime(headers.get('retry-after',''))
                if until.tzinfo is None:until=until.replace(tzinfo=timezone.utc)
                retry=max(0.,(until-datetime.now(timezone.utc)).total_seconds())
            except (ValueError,TypeError,OverflowError):retry=0.
        if not math.isfinite(retry):retry=0.
        with self.lock:
            now=self.clock();self.metrics['http_responses']+=1
            for name,value in values.items():
                scope='global' if name=='x-app-usage' else account
                self.usage[(name,scope)]=(value,now)
                self.observed=True
                metric=name.replace('-','_')+'_max_percent'
                self.metrics[metric]=max(self.metrics[metric],value)
            recent=[v for v,t in self.usage.values() if now-t<=120]
            pressure=max(recent,default=0)
            if limited or pressure>=70:
                self.limit=max(1,self.limit//2);self.last_change=now;self.metrics['reductions']+=1
                # Any explicit rate limit pauses all new requests; the limit
                # can be shared across identities/apps. No token rotation.
                if limited:
                    self.metrics['rate_limit_responses']+=1
                    self.global_until=max(self.global_until,now+max(60,retry,regain))
                if values.get('x-app-usage',0)>=85:
                    self.global_until=max(self.global_until,now+max(60,regain))
                if max(values.get('x-ad-account-usage',0),values.get('x-business-use-case-usage',0))>=85:
                    self.account_until[account]=max(self.account_until.get(account,0),now+max(60,regain))
            elif values and recent and pressure<60 and now-self.last_change>=20:
                self.limit=min(self.maximum,self.limit+2);self.last_change=now
                self.metrics['increases']+=1

    def snapshot(self):
        with self.lock:return dict(self.metrics,concurrency_limit=self.limit,maximum=self.maximum,
            max_in_flight=self.max_seen,active=self.active,global_cooldown_seconds=max(0,round(self.global_until-self.clock(),1)))


class GovernedSession:
    def __init__(self,session,governor,stop):
        self.session=session;self.governor=governor;self.stop=stop;self.account='';self.write_check=lambda:None

    def request(self,method,*args,**kwargs):
        with self.governor.slot(self.account,self.stop):
            if method=='DELETE':self.write_check()
            response=self.session.request(method,*args,**kwargs)
            self.governor.observe(response,self.account)
            return response

    def close(self):
        if hasattr(self.session,'close'):self.session.close()
