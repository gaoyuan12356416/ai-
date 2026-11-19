"""Deterministic exploration of eligible content; no new publishing writes."""
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from features.x_tag_revenue.core import language as canonical_language
from features.x_tag_revenue.taxonomy import THEMES, UNKNOWN
from .store import dumps
from .model import VERSION


def catalog_themes(metadata):
    catalog = metadata.get('catalog') or 'dramawave'
    return [(catalog, theme) for theme in metadata.get('themes', ()) if theme in THEMES and theme != UNKNOWN]


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
        self.language=canonical_language(account.get('drama_language')) or 'en'
        self.identity=identity
        self.context=context
        self.profile=context.get('profile') or {}
        self.meta={'version':self.profile.get('version','')}
        self.seed=dumps([VERSION,self.account_id,identity,self.meta['version']])
        self.nodes=[n for n in self.profile.get('themes',[]) if canonical_language(n['language'])==self.language]
        self.mature_pairs = {(n.get('catalog') or 'dramawave', n['theme']) for n in self.nodes if n['mature']}
        self.cold_positions = {}
        self.cold_roles = {}
        self.cold_evidence = {}
        self.order=[]
        self.role='explore'
        self.reason='' if self.mature_pairs else 'growth_learning'
        self.weights={}
        self.node_weights={}
        self.orders={}
        for node in self.nodes:
            if node['mature']:
                a=node['audience'];t=node['theme']
                # Exact-material assignment shares the integer-based revenue scorer.
                weight = round(node['score'] * 100)
                self.weights[a] = self.weights.get(a, 0) + weight
                self.node_weights.setdefault((a,), {})[t] = weight
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

    def prepare_cold_start(self, available, recent):
        """Balance recommendations and exploration against actual past choices."""
        by_catalog = (self.profile.get('cold_start') or {}).get('by_catalog') or {}
        weights = {}
        for catalog, group in by_catalog.items():
            if group.get('language') != self.language:
                continue
            for recommendation in group.get('recommendations', ()):
                pair = (catalog, recommendation['theme'])
                if pair in available and recommendation['weight'] > 0:
                    weights[pair] = recommendation['weight']
                    self.cold_evidence[pair] = (group, recommendation)
        if not weights:
            return False

        # Count admitted selections. Preview, failed candidates, other products,
        # and unrelated profile modes cannot consume an exploration slot.
        observed = Counter()
        recommended = Counter()
        explored = 0
        for row in recent:
            pair = (row.get('growth_catalog') or (row.get('classification') or {}).get('catalog') or 'dramawave',
                    row.get('selected_tag_key'))
            if pair not in available:
                continue
            observed[pair] += 1
            if row.get('growth_cold_start'):
                if row.get('growth_role') == 'cold_start':
                    recommended[pair] += 1
                else:
                    explored += 1
        model_count = sum(recommended.values())
        total_weight = sum(weights.values())
        evidence_order = sorted(weights, key=lambda pair: (
            recommended[pair] - weights[pair] * (model_count + 1) / total_weight,
            observed[pair], self.tie(pair)))
        # Even when every theme has evidence, keep exploring the least-sampled
        # alternatives. With just one eligible theme there is no alternative.
        alternatives = available - weights.keys() or available - {evidence_order[0]}
        explore_order = sorted(alternatives, key=lambda pair: (observed[pair], self.counts[pair[1]], self.tie(pair)))
        next_total = model_count + explored + 1
        use_model = not explore_order or .6 * next_total - model_count >= .4 * next_total - explored
        groups = [('cold_start', evidence_order), ('explore', explore_order)]
        if not use_model:
            groups.reverse()
        for role, pairs in groups:
            for pair in pairs:
                if pair not in self.cold_positions:
                    self.cold_positions[pair] = len(self.cold_positions)
                    self.cold_roles[pair] = role
        self.order = list(dict.fromkeys(theme for _, theme in self.cold_positions))
        return True

    def prepare(self,metadata):
        self.order = []
        self.cold_positions.clear()
        self.cold_roles.clear()
        self.cold_evidence.clear()
        self.reason = '' if self.mature_pairs else 'growth_learning'
        pairs = {pair for value in metadata if value for pair in catalog_themes(value)}
        available = sorted({theme for _, theme in pairs})
        if not available:
            self.reason='growth_labels_unavailable'
            return
        recent = [r for r in recent_choices(int(self.account_id)) if canonical_language(r.get('language')) == self.language]
        mature = {theme for _, theme in self.mature_pairs & pairs}
        if not mature:
            if self.prepare_cold_start(pairs, recent):
                return
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
        counts=Counter(r.get('selected_tag_key') for r in recent)
        total=sum(counts.values())
        self.order=sorted(weights,key=lambda t:(-(weights[t]*(total+1)-counts[t]),self.counts[t],self.tie(t)))
        self.order += [t for t in available if t not in self.order]
        chosen=self.order[0]
        self.role='exploit' if chosen in mature and (self.profile.get('policy') or {}).get('weights',{}).get(chosen,0)>0 else 'explore'

    def rank(self,metadata):
        if self.cold_positions:
            return (min((self.cold_positions.get(pair, len(self.cold_positions))
                         for pair in catalog_themes(metadata or {})), default=len(self.cold_positions)), 0)
        policy_weights = (self.profile.get('policy') or {}).get('weights', {})
        positions = [self.order.index(theme) for catalog, theme in catalog_themes(metadata or {})
                     if theme in self.order and (not policy_weights.get(theme) or (catalog, theme) in self.mature_pairs)]
        return (min(positions) if positions else len(self.order),0)

    def candidate_rank(self,metadata,content_id):
        return self.rank(metadata)+(self.drama_counts[str(content_id)],)

    def preferred(self):
        return []

    def audit(self,metadata,*,fallback=''):
        metadata=metadata or {}
        # Exact-drama/material matching uses the mature audience-theme weights
        # directly and never runs the ordinary exploration ranking.
        ranking = self.order or self.orders.get((metadata.get('audience'),), [])
        theme = next((t for t in ranking if t in metadata.get('themes', [])), '')
        role = 'strict_match' if theme and not self.order else (self.role if self.order and theme == self.order[0] else 'explore')
        details = {}
        if self.cold_positions:
            candidates = [pair for pair in catalog_themes(metadata) if pair in self.cold_positions]
            chosen = min(candidates, key=self.cold_positions.get) if candidates else None
            theme = chosen[1] if chosen else ''
            role = self.cold_roles.get(chosen, 'explore')
            details = dict(growth_cold_start=True, growth_catalog=metadata.get('catalog') or 'dramawave')
            if role == 'cold_start':
                group, recommendation = self.cold_evidence[chosen]
                details.update(growth_source=group['source'], growth_stage=group['stage'],
                               growth_evidence_posts=recommendation['posts'], growth_evidence_accounts=recommendation['accounts'],
                               growth_evidence_dramas=recommendation['dramas'], growth_score=recommendation['score'])
        elif role == 'exploit' and (metadata.get('catalog') or 'dramawave', theme) not in self.mature_pairs:
            role = 'explore'
        return dict(policy_version=VERSION,profile_mode='growth',mode_version=self.context.get('mode_version',0),
                    profile_version=self.profile.get('version',''),profile_generated_at=self.profile.get('computed_at',''),
                    account_id=self.account_id,x_user_id=self.x_user_id,language=self.language,
                    classification=metadata,selected_tag_key=theme,selected_tag=THEMES.get(theme,''),
                    selected_path_names=[THEMES[theme]] if theme in THEMES else [],
                    growth_role=role,
                    preferred_tag=THEMES.get(self.order[0],'') if self.order else '',
                    fallback=bool(fallback or not theme),fallback_reason=fallback or ('' if theme else 'growth_labels_unavailable'),
                    growth_mature_posts=self.profile.get('mature_posts',0),growth_policy=self.profile.get('policy',{}),
                    followers=self.profile.get('followers',{}),weights=[],
                    explanation={'exploit':'成长画像：成熟题材优先','strict_match':'成长画像：指定素材与成熟题材匹配',
                                 'cold_start':'成长画像：依据冷启动样本推荐题材','explore':'成长画像：探索或补足题材样本'}[role], **details)

def choose_context(account):
    return account.get('selection_profile') or {'mode': 'revenue', 'profile': {}}
