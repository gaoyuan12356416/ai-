import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from features.fb_auto_posts.effect_collector import FeedbackError
from features.social_material_effects import backfill,data,graph


def post(i,page='10',native=True):
    key=page+'_'+str(i)
    return dict(post_id=key,account_id=page,object_id=str(i),native=key if native else '',
                published_at='2026-09-01T00:00:00+00:00')


class Reader:
    def __init__(self,pages):
        self.pages=pages
    def credentials(self,ids):
        return {p:self.pages.get(p,[]) for p in ids}


class FakeGraph:
    def __init__(self,fn,budget=300):
        self.fn=fn
        self.calls=0
        self.max_calls=budget
        self.group_sizes=[]
    def get(self,params,token):
        if self.calls>=self.max_calls:
            raise FeedbackError('feedback_request_budget_reached')
        self.calls+=1
        keys=params['ids'].split(',')
        self.group_sizes.append(len(keys))
        return self.fn(keys,params['fields'],token)


def metrics(keys,fields,token):
    return {k:dict(id=k,reactions={'summary':{'total_count':0}},comments={'summary':{'total_count':4}}) for k in keys}


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        graph.prepare(self.db)
        self.reader=Reader({'10':[{'value':'one'},{'value':'two'}]})
        self.usage=graph.Usage()
        self.now=int(datetime.now(timezone.utc).timestamp())
    def run_collect(self,rows,g,history=True):
        return graph.collect(self.db,{p['post_id']:p for p in rows},history,g,self.reader,self.usage,6000,self.now)
    def test_oldest_unseen_multiple_batches_and_successful_zero(self):
        old=[post(i) for i in range(1,64)]
        recent=post(99,'22')
        recent['published_at']='2026-11-26T00:00:00+00:00'
        g=FakeGraph(metrics)
        ans=self.run_collect([recent,*old],g)
        self.assertEqual(g.group_sizes,[25,25,13])
        self.assertEqual((ans['first_attempted'],ans['first_covered']),(64,63))
        self.assertEqual(ans['errors'],{'fb_page_access_unavailable':1})
        row=self.db.execute("select metrics from observation where id='10_1'").fetchone()
        vals=json.loads(row[0]);times=vals.pop('_times')
        self.assertEqual(vals,{'likes':0,'comments':4})
        self.assertEqual(set(times),{'likes','comments'})
        self.assertEqual(self.run_collect(old,FakeGraph(metrics))['graph_calls'],0)
    def test_mid_batch_budget_does_not_mark_unread_posts(self):
        def fn(keys,fields,token):
            return {k:{'id':k,'post_id':'10_'+k} for k in keys} if fields=='id,post_id' else metrics(keys,fields,token)
        rows=[post(1,native=False),post(2,native=False)]
        g=FakeGraph(fn,budget=1)
        self.assertEqual(self.run_collect(rows,g)['first_attempted'],0)
        self.assertEqual(self.db.execute('select count(*) from observation').fetchone()[0],0)
        self.assertEqual(self.run_collect(rows,FakeGraph(fn))['first_covered'],2)
    def test_quota_suspends_without_retrying_another_page_credential(self):
        def limited(*args):
            raise FeedbackError('feedback_graph_32')
        g=FakeGraph(limited)
        self.usage.wait=2400
        stats=self.run_collect([post(1),post(2,'20')],g)
        self.assertEqual((stats['metrics_upstream_error'],g.calls),('feedback_graph_32',1))
        self.assertEqual(self.db.execute('select count(*) from observation').fetchone()[0],0)
        self.assertGreaterEqual(self.db.execute("select value from collection_state where key='resume_at'").fetchone()[0],self.now+2400)
    def test_missing_fields_keep_last_known_measurements(self):
        self.db.execute('INSERT INTO observation VALUES(?,?,?,?,?,?)',('10_1','10_1','{"shares":3,"views":5}','2026-09-01',0,''))
        stats=self.run_collect([post(1)],FakeGraph(metrics),False)
        self.assertEqual(stats['first_covered'],1)
        vals=json.loads(self.db.execute('select metrics from observation').fetchone()[0])
        times=vals.pop('_times')
        self.assertEqual((times['shares'],times['views']),('2026-09-01','2026-09-01'))
        self.assertNotEqual(times['likes'],times['views'])
        self.assertEqual(vals,{'likes':0,'comments':4,'shares':3,'views':5,'_views_at':'2026-09-01'})
        rows={'10_1':dict(post(1),views=8,metrics_at='2026-10-01')}
        graph.apply_cached(self.db,rows)
        self.assertEqual(rows['10_1']['views'],8)
        self.assertNotIn('_views_at',rows['10_1'])
    def test_denied_is_unknown_and_other_page_continues(self):
        self.reader.pages['22']=[{'value':'good'}]
        def fn(keys,fields,token):
            if token!='good':
                raise FeedbackError('feedback_graph_10')
            return metrics(keys,fields,token)
        stats=self.run_collect([post(1),post(2,'22')],FakeGraph(fn))
        self.assertEqual((stats['first_attempted'],stats['first_covered']),(2,1))
        self.assertEqual(stats['errors'],{'feedback_graph_10':1})
        vals=json.loads(self.db.execute("select metrics from observation where id='10_1'").fetchone()[0])
        self.assertFalse(any(k in vals for k in ('views','likes','comments','shares')))
    def test_usage_header_and_retry_after(self):
        r=SimpleNamespace(status_code=429,headers={'Retry-After':'120','x-business-use-case-usage':
              '{"page":[{"call_count":88,"estimated_time_to_regain_access":8}]}'})
        self.usage.response(r)
        self.assertEqual((self.usage.http_status,self.usage.peak,self.usage.wait),(429,88,480))

    def test_candidates_from_confirmed_graph_and_video_links(self):
        rows=[dict(post(i),namespace='ads_custom_source',material_id=str(i),post_url=url)
              for i,url in [(1,'https://www.facebook.com/10/posts/111'),(2,'https://www.facebook.com/watch/?v=2'),
                            (3,'https://www.facebook.com/11/posts/333')]]
        with tempfile.TemporaryDirectory() as tmp:
            data.publish(tmp,'facebook',rows,{})
            got=backfill.candidates(tmp)
        self.assertEqual([got['10_'+str(i)]['native'] for i in (1,2,3)],['10_111','',''])

    @unittest.skipUnless(os.name=='posix','production uses POSIX advisory locks')
    def test_hourly_refresh_reads_completed_cache_while_history_has_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp)/'obs.sqlite3'
            with sqlite3.connect(cache) as db:
                graph.prepare(db)
                db.execute('insert into observation values (?,?,?,?,?,?)',('10_1','10_1','{"likes":0,"comments":7}','2026-11-01',0,''))
            with cache.with_suffix('.lock').open('a') as guard:
                fcntl.flock(guard,fcntl.LOCK_EX)
                rows={'10_1':post(1)}
                stats=graph.sync(rows,cache)
            self.assertEqual(stats['graph_calls'],0)
            self.assertEqual(stats['collection_status'],'other_collector_active')
            self.assertEqual((rows['10_1']['likes'],rows['10_1']['comments']),(0,7))
