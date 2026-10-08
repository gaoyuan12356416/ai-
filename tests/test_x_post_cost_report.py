import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from features.post_daily_report.common import Window
from features.post_daily_report.x_cost import collect_cost
from features.post_daily_report.report import build_card, build_compact_card


class XCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {k: str(Path(self.tmp.name) / (k + '.db')) for k in ('x', 'x_auto')}
        self.db = sqlite3.connect(self.paths['x'])
        self.auto = sqlite3.connect(self.paths['x_auto'])
        self.addCleanup(self.db.close)
        self.addCleanup(self.auto.close)
        self.db.executescript('''CREATE TABLE x_post_queue(id INTEGER,delivery_mode TEXT);
          CREATE TABLE x_post_publish_log(queue_id INTEGER,status TEXT,x_post_id TEXT,post_text TEXT,published_at TEXT,unknown_outcome INTEGER,started_at TEXT,attempt_count INTEGER);
          CREATE TABLE x_post_repost_ledger(queue_id INTEGER,status TEXT,source_post_id TEXT,source_published_at TEXT,reposted_at TEXT,unknown_outcome INTEGER);''')
        self.auto.execute('''CREATE TABLE x_auto_task(status TEXT,execution_queue_id INTEGER,publish_id TEXT,published_at_utc TEXT,unknown_outcome INTEGER,post_text TEXT)''')
        self.window = Window.for_date('2026-09-07')

    def post(self, queue, text, stamp='2026-09-07T01:00:00Z', status='published', mode='direct', unknown=0):
        self.db.execute('INSERT INTO x_post_queue VALUES(?,?)', (queue, mode))
        self.db.execute('INSERT INTO x_post_publish_log VALUES(?,?,?,?,?,?,?,1)', (queue,status,str(1000+queue),text,stamp,unknown,stamp))

    def cost(self):
        self.db.commit(); self.auto.commit()
        return collect_cost(self.paths,self.window.start,self.window.end)

    def test_url_body_and_plain_body_use_distinct_rates(self):
        self.post(1,'Watch https://example.com/a')
        self.post(2,'Watch the drama now')
        result=self.cost()
        self.assertEqual(result['estimated_usd'],'0.215')
        self.assertEqual(result['counts'],{'post_with_url':1,'post_without_url':1,'repost':0})
        self.assertIn('估算',result['note'])

    def test_auto_bridge_and_duplicate_post_id_count_once(self):
        self.post(1,'https://example.com')
        self.auto.execute('INSERT INTO x_auto_task VALUES(?,?,?,?,?,?)',('published',1,'1001','2026-09-07T01:00:00Z',0,'https://example.com'))
        self.auto.execute('INSERT INTO x_auto_task VALUES(?,?,?,?,?,?)',('published',None,'1001','2026-09-07T01:00:00Z',0,'https://example.com'))
        self.assertEqual(self.cost()['estimated_usd'],'0.200')

    def test_relay_original_is_charged_even_when_target_fails(self):
        self.post(1,'https://example.com',status='source_published',mode='premium_relay_repost')
        self.db.execute('INSERT INTO x_post_repost_ledger VALUES(?,?,?,?,?,?)',(1,'failed','source1','2026-09-07T01:00:00Z','',0))
        result=self.cost()
        self.assertEqual(result['estimated_usd'],'0.200')
        self.assertEqual(result['source_counts'],{'relay_original':1})

    def test_relay_target_and_original_are_separate_operations(self):
        self.post(1,'https://example.com',mode='premium_relay_repost')
        self.db.execute('INSERT INTO x_post_repost_ledger VALUES(?,?,?,?,?,?)',(1,'reposted','1001','2026-09-07T01:00:00Z','2026-09-07T01:01:00Z',0))
        result=self.cost()
        self.assertEqual(result['estimated_usd'],'0.215')
        self.assertEqual(result['counts']['post_with_url'],1)
        self.assertEqual(result['counts']['repost'],1)

    def test_actual_beijing_day_excludes_next_day_catchup(self):
        self.post(1,'https://example.com','2026-09-06T15:59:59Z')
        self.post(2,'https://example.com','2026-09-06T16:00:00Z')
        self.post(3,'https://example.com','2026-09-07T16:00:00Z')
        self.assertEqual(self.cost()['estimated_usd'],'0.200')

    def test_reserved_failed_and_unknown_are_not_confirmed_writes(self):
        self.post(1,'https://example.com',status='reserved')
        self.post(2,'https://example.com',status='failed')
        self.post(3,'https://example.com',unknown=1)
        result=self.cost()
        self.assertEqual(result['estimated_usd'],'0.000')
        self.assertEqual(result['unconfirmed_attempts'],1)

    def test_missing_body_is_unpriced_and_missing_db_is_unknown(self):
        self.post(1,None)
        result=self.cost()
        self.assertIsNone(result['estimated_usd'])
        self.assertEqual(result['unpriced_posts'],1)
        result=collect_cost(dict(self.paths,x_auto=str(Path(self.tmp.name)/'missing.db')),self.window.start,self.window.end)
        self.assertIsNone(result['estimated_usd'])
        self.assertEqual(result['unreadable_sources'],['x_auto'])

    def test_normal_and_compact_cards_include_cost_and_basis(self):
        self.post(1,'https://example.com')
        report={'date':self.window.date,'channels':[{'channel':'X','rows':[], 'cost':self.cost()}]}
        for builder in (build_card,build_compact_card):
            card=json.dumps(builder(report),ensure_ascii=False)
            self.assertIn('US$0.20',card)
            self.assertIn('估算',card)
            self.assertIn('实际发布日',card)


if __name__=='__main__': unittest.main()
