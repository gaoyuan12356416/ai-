"""Comparable 72-hour evidence, drama-balanced scores and bounded weekly learning."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import math
import statistics

from .store import dumps

VERSION = 'x-growth-v1'
MIN_POSTS = 6
MIN_DRAMAS = 3

def instant(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)

def duration_band(seconds):
    if seconds is None:
        return 'unknown'
    return '0-5m' if seconds < 300 else '5-20m' if seconds < 1200 else '20m+'

def percentile(value, values):
    if value is None or not values:
        return None
    return 100 * (sum(v < value for v in values) + .5*sum(v == value for v in values)) / len(values)

def follower_summary(rows, now):
    rows = sorted(rows, key=lambda r: r['observed_at'])
    def at(days):
        target = now-timedelta(days=days)
        eligible = [r for r in rows if abs((instant(r['observed_at'])-target).total_seconds()) <= 8*3600]
        return min(eligible, key=lambda r: abs((instant(r['observed_at'])-target).total_seconds())) if eligible else None
    current, week, fortnight = at(0), at(7), at(14)
    net = current['followers']-week['followers'] if current and week else None
    prior = week['followers']-fortnight['followers'] if week and fortnight else None
    return dict(followers=current['followers'] if current else None, net_7d=net, previous_net_7d=prior,
                basis='account_net_change_not_post_attribution', two_weeks_nonpositive=net is not None and prior is not None and net <= 0 and prior <= 0)

def build_profiles(posts, followers, previous=None, now=None):
    now = now or datetime.now(timezone.utc)
    previous = previous or {}
    rows = []
    accounts = set(followers) | set(previous)
    for post in posts:
        accounts.add(int(post['account_id']))
        age = (instant(post['observed_at'])-instant(post['published_at'])).total_seconds()/3600
        if post.get('stage') != '72h' or not 72 <= age <= 78 or instant(post['published_at']) < now-timedelta(days=28):
            continue
        m = post['metrics']
        impressions = m.get('organic_impressions')
        # A public total cannot safely replace organic data for a promoted post.
        if impressions is None and m.get('promoted_impressions', 0) == 0:
            impressions = m.get('impressions')
        if impressions is None or impressions < 100:
            continue
        clicks = m.get('profile_clicks')
        interactions = m.get('organic_reposts')
        bookmarks = m.get('bookmarks')
        if interactions is None:
            interactions = m.get('reposts') if m.get('promoted_impressions', 0) == 0 else None
        # Smoothed rates keep 1 click / very few impressions from winning.
        metrics = [math.log1p(impressions), (clicks/(impressions+200)) if clicks is not None else None,
                   ((interactions+bookmarks)/(impressions+200)) if interactions is not None and bookmarks is not None else None]
        row = dict(post, values=metrics, band=duration_band(post.get('duration')))
        row['metric_mask'] = tuple(v is not None for v in metrics)
        rows.append(row)
    groups = defaultdict(list)
    for row in rows:
        peers = [r for r in rows if (r['catalog'],r['language'],r['band'],r['metric_mask']) == (row['catalog'],row['language'],row['band'],row['metric_mask'])]
        own = [r for r in peers if r['account_id'] == row['account_id']]
        reference = own if len(own) >= 10 else peers
        weights = (60,25,15)
        scored = [(percentile(value,[r['values'][i] for r in reference if r['values'][i] is not None]), weights[i]) for i,value in enumerate(row['values']) if value is not None]
        score = sum(v*w for v,w in scored)/sum(w for _,w in scored)
        metadata = row.get('metadata') or {}
        themes = sorted(set(metadata.get('themes') or [])-{'__unknown__'})
        audience = metadata.get('audience', '__unknown__')
        for theme in themes:
            groups[(int(row['account_id']), row['catalog'], row['language'], audience, theme)].append((row,score,1/len(themes)))
    profiles = {}
    for account_id in accounts:
        nodes=[]
        for (aid,catalog,language,audience,theme), samples in groups.items():
            if aid != account_id:
                continue
            dramas=defaultdict(list)
            for row,score,credit in samples:
                dramas[row['content_id']].append(score)
            score=statistics.mean(statistics.median(values) for values in dramas.values())
            # Shrink sparse evidence toward neutral; multi-tag posts share credit.
            n=sum(credit for _,_,credit in samples)
            score=(n*score+6*50)/(n+6)
            nodes.append(dict(catalog=catalog,language=language,audience=audience,theme=theme,score=round(score,2),
                              posts=len(samples),effective_posts=round(n,2),dramas=len(dramas),
                              mature=n>=MIN_POSTS and len(dramas)>=MIN_DRAMAS,
                              drama_counts={key:len(value) for key,value in dramas.items()}))
        nodes.sort(key=lambda r:(-r['score'],r['theme']))
        growth=follower_summary(followers.get(account_id,[]),now)
        old=previous.get(account_id,{})
        due=not old.get('policy_updated_at') or (now-instant(old['policy_updated_at'])).days >= 7
        mature=[r for r in nodes if r['mature']]
        policy = old.get('policy') if not due else None
        if not policy:
            own_count=sum(int(r['account_id'])==account_id for r in rows)
            exploration=.4 if own_count>=18 and growth['two_weeks_nonpositive'] else .2
            target={}
            leaders=[]
            for node in mature:
                if node['theme'] not in {n['theme'] for n in leaders}:
                    leaders.append(node)
            clear_leader=bool(leaders and leaders[0]['score']>=55 and (len(leaders)==1 or leaders[0]['score']-leaders[1]['score']>=5))
            if clear_leader:
                target[leaders[0]['theme']]=.8-exploration
                if len(leaders)>1:
                    target[leaders[1]['theme']]=.2
                else:
                    exploration += .2
            else:
                exploration=1.
            previous_policy=old.get('policy')
            if previous_policy is not None:
                before=previous_policy.get('weights',{})
                keys=set(before)|set(target)
                # Interpolate complete allocations, including exploration. Every
                # component changes by at most twenty percentage points/week,
                # including the first transition out of pure exploration.
                delta=max([abs(target.get(k,0)-before.get(k,0)) for k in keys]
                          +[abs(exploration-previous_policy.get('exploration',1))])
                step=min(1,.2/delta) if delta else 1
                target={k:before.get(k,0)+step*(target.get(k,0)-before.get(k,0)) for k in keys}
                target={k:v for k,v in target.items() if v>1e-9}
                exploration=1-sum(target.values())
            policy=dict(weights=target,exploration=round(exploration,4))
        profile=dict(account_id=account_id,policy_version=VERSION,computed_at=now.isoformat(),
                     policy_updated_at=now.isoformat() if due else old['policy_updated_at'],
                     themes=nodes,policy=policy,followers=growth,
                     mature_posts=len([r for r in rows if int(r['account_id'])==account_id]),
                     score_basis='72h_organic_post_impressions_profile_clicks_reposts_bookmarks',
                     note='媒体播放及观看时长仅辅助展示；涨粉是账号净变化，不归因到单条帖子。')
        profile['version']=hashlib.sha256(dumps(profile).encode()).hexdigest()[:16]
        profiles[account_id]=profile
    return profiles
