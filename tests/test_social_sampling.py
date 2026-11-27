from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from features.social_material_effects import sampling,data,graph,report
from tests.test_social_material_daily import post
from tests.test_fb_social_history import FakeGraph,Reader,metrics,post as fbpost


class SamplingTest(unittest.TestCase):
    def test_closing_hour_is_beijing_and_never_crosses_day(self):
        self.assertIsNone(sampling.closing_start(datetime(2026,11,27,12,59,tzinfo=timezone.utc)))
        self.assertEqual(sampling.closing_start(datetime(2026,11,27,15,30,tzinfo=timezone.utc)).isoformat(),
                         '2026-11-27T21:00:00+08:00')
        self.assertIsNone(sampling.closing_start(datetime(2026,11,27,14,59,tzinfo=timezone.utc),23))
        self.assertIsNone(sampling.closing_start(datetime(2026,11,27,16,tzinfo=timezone.utc)))

    def test_daily_coverage_and_close_bypass_success_interval_but_not_failure_retry(self):
        now=int(datetime(2026,11,27,15,10,tzinfo=timezone.utc).timestamp())
        rows=[fbpost(i) for i in range(4)]
        saved={p['post_id']:(p['post_id'],'','{}','2026-11-27T12:00:00Z',now+3600,'') for p in rows}
        saved['10_1']=('10_1','','{}','2026-11-27T15:01:00Z',now+3600,'')
        saved['10_2']=('10_2','','{}','2026-11-26T14:00:00Z',now+3600,'feedback_graph_10')
        due,groups=graph.plan({p['post_id']:p for p in rows},saved,True,now,100,coverage=True)
        self.assertEqual(due,2)
        self.assertEqual([p['post_id'] for p in groups['10']],['10_0','10_3'])
        daytime=int(datetime(2026,11,27,8,tzinfo=timezone.utc).timestamp())
        self.assertEqual(graph.plan({p['post_id']:p for p in rows},saved,True,daytime,100,coverage=True)[0],0)

    def test_permission_page_is_skipped_but_other_page_and_history_progress(self):
        from features.fb_auto_posts.effect_collector import FeedbackError
        def request(keys,fields,token):
            if token=='bad':raise FeedbackError('feedback_graph_10')
            return metrics(keys,fields,token)
        with closing(sqlite3.connect(':memory:')) as db:
            graph.prepare(db)
            posts=[*[fbpost(i) for i in range(70)],fbpost(99,'22')]
            g=FakeGraph(request)
            ans=graph.collect(db,{p['post_id']:p for p in posts},True,g,
                Reader({'10':[{'value':'bad'}],'22':[{'value':'ok'}]}),graph.Usage(),100,100,True)
            self.assertEqual(g.calls,2)
            self.assertEqual(ans['attempted'],26)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM counter_history').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT page FROM page_retry').fetchone()[0],'10')

    def test_empty_metric_response_does_not_cancel_page_permission_cooldown(self):
        from features.fb_auto_posts.effect_collector import FeedbackError
        def request(keys,fields,token):
            if token=='empty':return {key:dict(id=key) for key in keys}
            if token=='denied':raise FeedbackError('feedback_graph_10')
            return metrics(keys,fields,token)
        with closing(sqlite3.connect(':memory:')) as db:
            graph.prepare(db)
            posts=[*[fbpost(i) for i in range(70)],fbpost(99,'22')]
            g=FakeGraph(request)
            reader=Reader({'10':[{'value':'empty'},{'value':'denied'}],'22':[{'value':'ok'}]})
            ans=graph.collect(db,{p['post_id']:p for p in posts},True,g,
                reader,graph.Usage(),100,100,True)
            self.assertEqual(g.calls,3)
            self.assertEqual(ans['attempted'],26)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM counter_history').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT page FROM page_retry').fetchone()[0],'10')
            # A second pass must leave the denied Page alone while it cools.
            second=FakeGraph(request)
            graph.collect(db,{p['post_id']:p for p in posts},True,second,
                reader,graph.Usage(),100,101,True)
            self.assertEqual(second.calls,0)

    def test_recovery_and_multiple_samples_keep_actual_day_and_latest_value(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'facebook-observations.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                sampling.prepare(db)
                sampling.save(db,'1',dict(views=100,likes=5),'2026-11-24T15:40:00Z')
                sampling.save(db,'1',dict(views=130,likes=7),'2026-11-25T07:00:00Z')
                sampling.save(db,'1',dict(views=160,likes=9),'2026-11-25T15:45:00Z')
                sampling.save(db,'1',dict(views=200,likes=11),'2026-11-26T15:45:00Z')
                sampling.save(db,'1',dict(views=9999),'2026-11-26T16:01:00Z')
                # A sample for a post not yet in the projection must survive.
                sampling.save(db,'2',dict(views=50),'2026-11-25T15:00:00Z')
                db.commit()
            meta=dict(version='v',generated_at='2026-11-27T09:00:00Z')
            data.publish(root,'facebook',[post()],meta)
            query=dict(channel=['facebook'],start=['2026-11-25'],end=['2026-11-26'])
            result=report.query(query,{'role':'admin'},root)
            self.assertEqual(result['summary']['views'],100)
            self.assertEqual([r['known']['views'] for r in result['items']],[40,60])
            data.publish(root,'facebook',[post(),post('2')],meta)
            with closing(sqlite3.connect(root/'facebook.sqlite3')) as db:
                self.assertEqual(db.execute("SELECT value FROM counter_day WHERE post_id='2'").fetchone()[0],50)
                self.assertEqual(json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])['history_samples_merged'],1)
            # A recovered earlier sample still advances the sequence watermark.
            with closing(sqlite3.connect(path)) as db:
                sampling.save(db,'1',dict(views=80),'2026-11-23T15:45:00Z');db.commit()
            data.publish(root,'facebook',[post(),post('2')],meta)
            with closing(sqlite3.connect(root/'facebook.sqlite3')) as db:
                self.assertEqual(db.execute("SELECT value FROM counter_day WHERE day='2026-11-23'").fetchone()[0],80)

    def test_missing_day_and_counter_rollback_are_unknown_after_history_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with closing(sqlite3.connect(root/'facebook-observations.sqlite3')) as db:
                sampling.prepare(db)
                sampling.save(db,'1',{'views':100},'2026-11-24T15:40:00Z')
                sampling.save(db,'1',{'views':200},'2026-11-26T15:40:00Z')
                sampling.save(db,'2',{'views':50},'2026-11-25T15:40:00Z')
                sampling.save(db,'2',{'views':40},'2026-11-26T15:40:00Z');db.commit()
            data.publish(root,'facebook',[post(),post('2')],{'generated_at':'2026-11-27T09:00:00Z'})
            result=report.query(dict(channel=['facebook'],start=['2026-11-26'],end=['2026-11-26']),{'role':'admin'},root)
            self.assertIsNone(result['summary']['views'])
            self.assertEqual(result['coverage']['views'],0)

    def test_x_close_refreshes_old_post_and_records_response_time(self):
        from features.x_material_effects import collector
        now=datetime(2026,11,27,15,30,tzinfo=timezone.utc)
        response_at=datetime(2026,11,27,15,31,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'obs.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE observations(post_id TEXT PRIMARY KEY,metrics_json TEXT,metrics_at TEXT,attempt_at TEXT,error_code TEXT,retry_at TEXT)')
                db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',('1','{"views":10}','2026-11-27T14:00:00Z','2026-11-27T14:00:00Z','',''))
                db.commit()
            p=dict(post(),author_id='a')
            with patch.object(collector,'datetime') as clock,patch.object(collector,'credentials',return_value='test'),patch.object(collector,'request') as request:
                clock.now.side_effect=[now,response_at]
                request.return_value={'data':[{'id':'1','author_id':'a','public_metrics':{'impression_count':25}}]}
                result=collector.collect_metrics([p],path,closing_pass=True)
            self.assertEqual(result['metrics_updated'],1)
            with closing(sqlite3.connect(path)) as db:
                stamp,raw=db.execute('SELECT observed_at,metrics FROM counter_history').fetchone()
                self.assertEqual(stamp,response_at.isoformat())
                self.assertEqual(json.loads(raw)['views'],25)

    def test_cache_seeding_closes_read_before_writes_need_exclusive_lock(self):
        from scripts.recover_social_daily_samples import import_file
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'observations.sqlite3'
            with closing(sqlite3.connect(path,timeout=0.1)) as db:
                graph.prepare(db)
                db.executemany('INSERT INTO observation VALUES(?,?,?,?,?,?)',
                    ((str(i),'',json.dumps({'views':i,'likes':i}),'2026-11-26T15:30:00Z',0,'') for i in range(1000)))
                db.commit();db.execute('PRAGMA cache_size=10')
                self.assertEqual(import_file(db,path,'facebook'),1000)
                db.commit()
                self.assertEqual(db.execute('SELECT COUNT(*) FROM counter_history').fetchone()[0],1000)


if __name__=='__main__':unittest.main()
