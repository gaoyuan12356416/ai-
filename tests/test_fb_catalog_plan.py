import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.fb_auto_posts import catalog_plan as catalog
from features.fb_auto_posts.core import FBAutoPostStore
from features.fb_auto_posts.metrics import MetricTotals, MetricWindow


class PlanTests(unittest.TestCase):
    def test_xml_control_character_fallback_checks_the_same_replica(self):
        bad = catalog.RepositoryError('source_readonly_check_failed','fixture',503)
        cat = catalog.Catalog.__new__(catalog.Catalog)
        original = '文\x01案'
        sql = 'SELECT name,body FROM texts WHERE id=%s'
        with patch.object(cat,'select',side_effect=[bad,[{'name':original.encode().hex(),'body':None}]]) as calls:
            self.assertEqual(cat.read_text(sql,(7,),('name','body')), [{'name':original,'body':None}])
            self.assertEqual(calls.call_args.args[1], (7,))
            self.assertIn(sql, calls.call_args.args[0])
        with patch.object(cat,'select',side_effect=bad), self.assertRaises(catalog.RepositoryError):
            cat.read_text(sql,(7,),('name','body'))

    def test_reconnect_replays_only_current_select(self):
        mysql=SimpleNamespace(schema='main',blacklist_schema='blocked')
        with patch.object(catalog,'BrokerReadOnlyMySQL',return_value=mysql), patch.object(catalog.time,'sleep'):
            cat=catalog.Catalog(None,dict(product='Dramawave'),{('en',False)},datetime.now(timezone.utc),None)
            with patch.object(mysql,'select',create=True,side_effect=[
                    catalog.TemplateError('source_query_failed','connection closed',503), [{'hi':'9'}]]) as read:
                self.assertEqual(cat.select('SELECT MAX(id) hi FROM t'),[{'hi':'9'}])
                self.assertEqual(read.call_count,2)

    def test_ready_order_and_cancelled_request(self):
        with tempfile.TemporaryDirectory() as temp:
            store = FBAutoPostStore(Path(temp) / 'ledger.sqlite3', now_fn=lambda: datetime(2026,11,25,4,tzinfo=timezone.utc))
            with store.connect() as db:
                db.execute("""INSERT INTO fb_auto_template VALUES(1,'example','enabled',1,'',1,'','','','')""")
                db.executemany("""INSERT INTO fb_auto_due_slot(template_id,template_version,slot_key,
                    planned_publish_at_utc,status,trigger_type,available_at_utc,created_at_utc,updated_at_utc)
                    VALUES(1,1,?,?,?,?,?,'','')""", [
                    ('auto', '2026-11-25T00:00:00+00:00', 'pending', 'auto','2026-11-25T03:00:00+00:00'),
                    ('first', '2026-11-25T02:00:00+00:00','pending','manual',''),
                    ('second','2026-11-25T02:01:00+00:00','cancelled','manual','')])
                db.execute("UPDATE fb_auto_due_slot SET schedule_policy='target_midnight_v1' WHERE slot_key='auto'")
                db.commit()
            self.assertEqual(store.claim_due_slot('w')['slot_key'], 'first')
            self.assertEqual(store.claim_due_slot('w')['slot_key'], 'auto')
            self.assertIsNone(store.claim_due_slot('w'))

    def test_bulk_order_scope_and_missing_winner(self):
        rule = dict(sort_by='spend',sort_direction='desc',spend_min=None,spend_max=None,roas_min=None,roas_max=None)
        win = MetricWindow((7,), ('2026-11-24',), {'a': MetricTotals(Decimal(10))},
                           {('a','3'):MetricTotals(Decimal(7))})
        repo = SimpleNamespace(now_fn=lambda:datetime(2026,11,25,tzinfo=timezone.utc),
                               metric_store=SimpleNamespace(load_metric_window=lambda **k:win), candidate_limit=2,
                               mysql=SimpleNamespace(schema='main', blacklist_schema='blocked'))
        cfg = dict(product='Dramawave', metric_product='Dramawave',metric_platform=0,metric_window_days=1,
                   material_rule=rule, drama_rule=rule,drama_cooldown_hours=12,_profile_selection=True)
        facts = {cid:dict(name=cid,resource_type_v2='2',deploy_time=1,language='en') for cid in 'abc'}
        desc = {('a','en'):'one',('c','en'):'three'}
        labels = {(cid,'en'):dict(audience='y' if cid=='c' else 'x',themes=[]) for cid in 'abc'}
        groups = {('a',''):[1,2,3],('b','en'):[4],('c','en'):[5],('a','fr'):[6],('missing','en'):[7]}
        def hydrate(_, ids):
            valid = {1:('a',''),2:('a',''),4:('b','en'),5:('c','en'),6:('a','fr')}
            return {mid:(dict(cid=valid[mid][0],lang=valid[mid][1],product='DramawaveSource',name='',
                              tag_name='',video_duration=10), 'https://example.org/video')
                    for mid in ids if mid in valid}
        with patch.object(catalog, 'BrokerReadOnlyMySQL', return_value=repo.mysql), \
             patch.object(catalog.Catalog,'index',return_value=groups), \
             patch.object(catalog.Catalog,'drama_facts',return_value=(facts,desc,labels,'')), \
             patch.object(catalog.Catalog,'hydrate',hydrate):
            res = catalog.snapshots(repo,cfg,{('en',True),('en',False),('fr',False),('fr',True)})
        actual = {scope:[x.material_id for x in snap.candidates] for scope,snap in res.items()}
        self.assertEqual(actual, {('en',True):['1','2','5'],('en',False):['1','2','5'],
                                  ('fr',False):['6'], ('fr',True):[]})
        self.assertEqual(res['en',True].metric_generation_ids,(7,))

    def test_null_roas_sorts_last_in_both_directions(self):
        zero = MetricTotals()
        known = MetricTotals(Decimal(5), Decimal(0))
        for direction in ['asc','desc']:
            rule = dict(sort_by='roas',sort_direction=direction)
            self.assertLess(catalog.rank(known,rule),catalog.rank(zero,rule))

    def test_resource_variants_and_splitting(self):
        def query(sql, args):
            if 'SELECT app FROM' in sql:
                return [{'app':'android'}] if len(args)==1 else []
            if 'ads_drama_info d FORCE' in sql:
                if 'different' in args and 'same' in args:
                    raise catalog.RepositoryError('source_query_timeout','fixture',503)
                return [dict(id='10',content_id=args[0],app_id='1479',release_status='1',deploy_time='10',
                             language='en',name='d',series_code='s',resource_type='2',resource_type_v2='1')]
            return [dict(content_id=args[1],app_id='1479',language='en',description=text)
                    for text in (['A','a','A'] if args[1]=='different' else ['A','A'])]
        mysql=SimpleNamespace(schema='main',blacklist_schema='blocked',select=query)
        cfg=dict(product='Dramawave',app_id='1479',drama_rule={},message_template='{{desc}}')
        with patch.object(catalog,'BrokerReadOnlyMySQL',return_value=mysql), ThreadPoolExecutor(1) as pool:
            cat=catalog.Catalog(None,cfg,{('en',True)},datetime.now(timezone.utc),pool)
            facts,copy,_,_=cat.drama_facts(['different','same'])
        self.assertEqual(set(facts),{'same','different'})
        self.assertEqual(copy,{('same','en'):'A'})
