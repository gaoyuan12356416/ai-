"""Deterministic exploration of eligible content; no new publishing writes."""
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from features.x_tag_revenue.taxonomy import THEMES, AUDIENCES, UNKNOWN
from .store import dumps, selection_context
from .model import VERSION

def recent_choices(account_id):
    rows=[]
    for key, table in [('X_AUTO_POST_DB_PATH','x_auto_task'), ('X_POST_DB_PATH','x_tag_queue_selection')]:
        path=Path(os.getenv(key,''))
        if not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
                if table=='x_auto_task':
                    items=db.execute("SELECT selection_json,created_at FROM x_auto_task WHERE account_id=? AND selection_json<>'{}' ORDER BY id DESC LIMIT 40",(str(account_id),))
                else:
                    # Auto decisions already occur in x_auto_task; this table owns pool decisions.
                    items=db.execute('SELECT s.selection_json,q.created_at FROM x_tag_queue_selection s JOIN x_post_queue q ON q.id=s.queue_id WHERE s.account_id=? ORDER BY s.queue_id DESC LIMIT 40',(account_id,))
                for raw, created in items:
                    data=json.loads(raw)
                    data=data.get('tag_profile',data)
                    if data.get('profile_mode')=='growth':
                        rows.append((created,data))
        except (sqlite3.Error,ValueError):
            continue
    return [v for _,v in sorted(rows,key=lambda r:r[0],reverse=True)[:20]]

class GrowthDecision:
    def __init__(self,account,identity,context):
        self.account_id=str(account['id'])
        self.x_user_id=str(account.get('x_user_id',''))
        self.language=str(account.get('drama_language') or 'en')
        self.identity=identity
        self.context=context
        self.profile=context.get('profile') or {}
        self.meta={'version':self.profile.get('version','')}
        self.seed=dumps([VERSION,self.account_id,identity,self.meta['version']])
        self.nodes=[n for n in self.profile.get('themes',[]) if n['language']==self.language]
        self.order=[]
        self.role='explore'
        self.reason='' if self.nodes else 'growth_learning'
        self.weights={}
        self.node_weights={}
        self.orders={}
        for node in self.nodes:
            if node['mature']:
                a=node['audience'];t=node['theme']
                self.weights[a]=self.weights.get(a,0)+node['score']
                self.node_weights.setdefault((a,),{})[t]=node['score']
        self.audience_order=list(self.weights)
        self.orders[()]=self.audience_order
        for key,weights in self.node_weights.items():
            self.orders[key]=sorted(weights,key=lambda t:-weights[t])
        self.counts=Counter()
        self.drama_counts=Counter()
        for node in self.nodes:
            self.counts[node['theme']]+=node['effective_posts']
            for key,value in node.get('drama_counts',{}).items():
                self.drama_counts[key]=max(self.drama_counts[key],value)

    def tie(self,value):
        return hashlib.sha256((self.seed+'\0'+str(value)).encode()).hexdigest()

    def prepare(self,metadata):
        available=sorted({t for value in metadata if value for t in value.get('themes',[]) if t!=UNKNOWN})
        if not available:
            self.reason='growth_labels_unavailable'
            return
        mature={n['theme'] for n in self.nodes if n['mature']}
        if not mature:
            # A stable initial set per channel avoids testing all nine topics at once.
            initial=sorted(available,key=lambda t:hashlib.sha256((self.account_id+'\0'+t).encode()).hexdigest())[:3]
            weights={t:1/len(initial) for t in initial}
        else:
            policy=self.profile.get('policy') or {}
            weights={t:w for t,w in policy.get('weights',{}).items() if t in available and t in mature}
            explore=sorted(available,key=lambda t:(self.counts[t],self.tie(t)))
            alternatives=[t for t in explore if t not in weights] or explore
            allocation=(1-sum(weights.values()))/len(alternatives)
            for theme in alternatives:
                weights[theme]=weights.get(theme,0)+allocation
        recent=recent_choices(int(self.account_id))
        counts=Counter(r.get('selected_tag_key') for r in recent if r.get('language')==self.language)
        total=sum(counts.values())
        self.order=sorted(weights,key=lambda t:(-(weights[t]*(total+1)-counts[t]),self.counts[t],self.tie(t)))
        self.order += [t for t in available if t not in self.order]
        chosen=self.order[0]
        self.role='exploit' if chosen in mature and (self.profile.get('policy') or {}).get('weights',{}).get(chosen,0)>0 else 'explore'

    def rank(self,metadata):
        themes=(metadata or {}).get('themes',[])
        positions=[self.order.index(t) for t in themes if t in self.order]
        return (min(positions) if positions else len(self.order),0)

    def candidate_rank(self,metadata,content_id):
        return self.rank(metadata)+(self.drama_counts[str(content_id)],)

    def preferred(self):
        return []

    def audit(self,metadata,*,fallback=''):
        metadata=metadata or {}
        choices=[t for t in self.order if t in metadata.get('themes',[])]
        theme=choices[0] if choices else ''
        return dict(policy_version=VERSION,profile_mode='growth',mode_version=self.context.get('mode_version',0),
                    profile_version=self.profile.get('version',''),profile_generated_at=self.profile.get('computed_at',''),
                    account_id=self.account_id,x_user_id=self.x_user_id,language=self.language,
                    classification=metadata,selected_tag_key=theme,selected_tag=THEMES.get(theme,''),
                    selected_path_names=[THEMES[theme]] if theme in THEMES else [],
                    growth_role=self.role if self.order and theme==self.order[0] else 'explore',
                    preferred_tag=THEMES.get(self.order[0],'') if self.order else '',
                    fallback=bool(fallback or not theme),fallback_reason=fallback or ('' if theme else 'growth_labels_unavailable'),
                    growth_mature_posts=self.profile.get('mature_posts',0),growth_policy=self.profile.get('policy',{}),
                    followers=self.profile.get('followers',{}),weights=[],
                    explanation='成长画像：成熟题材优先' if self.role=='exploit' else '成长画像：探索或补足题材样本')

def choose_context(account):
    # Historic frozen tasks explicitly carry revenue below, so changing a setting
    # cannot reinterpret already created tasks.
    return account.get('selection_profile') or selection_context(account)
