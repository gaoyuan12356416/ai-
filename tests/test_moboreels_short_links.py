from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import tempfile
import threading
import unittest

from features.moboreels_library.short_links import ShortLinks, safe_target
from features.moboreels_library.macro_client import contains_macro
from features.moboreels_library.promotions import PromotionError, PromotionManager
from features.moboreels_library.settings import Settings
from test_moboreels_promotions import ACTOR, FakeDB, FakeProvider


class LinksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.settings = Settings(Path(tmp.name), 'institution', 'test', 'test', 'test', 'test')
        self.db, self.provider = FakeDB(), FakeProvider()
        self.manager = PromotionManager(self.settings, self.db, self.provider)
        self.links = ShortLinks(self.settings, self.db, self.manager)
        self.body = dict(actor=ACTOR, story_id='42', platform='x', account_id='acct', task_id='task-1', material_id='material-1', purpose='post')

    def test_same_context_and_name_change_reuses_link(self):
        first = self.links.resolve(self.body)
        second = self.links.resolve(dict(self.body, actor=dict(ACTOR, name='新名字')))
        self.assertEqual(first, second)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.links.target(first['short_url'].rsplit('/',1)[1]), first['target_url'])
        self.assertEqual(first['promotion_request_id'], self.provider.calls[0][1])

    def test_different_material_task_and_owner_attribution(self):
        first = self.links.resolve(self.body)
        second = self.links.resolve(dict(self.body, task_id='task-2', platform='fb'))
        self.assertNotEqual(first['short_link_id'], second['short_link_id'])
        self.assertEqual(first['promotion_key'], second['promotion_key'])
        self.assertEqual(first['koc_code'], second['koc_code'])
        other = self.links.resolve(dict(self.body, actor=dict(ACTOR, user_id='other')))
        self.assertNotEqual(other['promotion_key'], first['promotion_key'])
        self.assertEqual(len(self.provider.calls), 2)

    def test_unknown_outcome_never_recreates(self):
        self.provider.error = TimeoutError()
        for _ in range(2):
            with self.assertRaises(PromotionError): self.links.resolve(self.body)
        self.assertEqual(len(self.provider.calls), 1)
        with self.links.connect() as conn:
            row = conn.execute('SELECT short_code FROM links').fetchone()
        self.assertIsNone(self.links.target(row[0]))

    def test_story_change_rejected_before_network(self):
        self.links.resolve(self.body)
        with self.assertRaises(PromotionError): self.links.resolve(dict(self.body, story_id='43'))
        self.assertEqual(len(self.provider.calls), 1)

    def test_concurrent_success_and_failed_mirror_preserve_redirect(self):
        first = self.links.resolve(self.body)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.links.resolve(self.body), range(12)))
        self.assertTrue(all(row == first for row in results))
        self.db._write = lambda *args: (_ for _ in ()).throw(TimeoutError())
        with self.assertLogs(level='WARNING'): self.links.flush_outbox()
        self.assertEqual(self.links.resolve(self.body), first)
        self.assertEqual(len(self.provider.calls),1)

    def test_cold_concurrent_tasks_share_pending_promotion_and_keep_short_code(self):
        entered, release = threading.Event(), threading.Event()
        original = self.provider.create_promotion

        def delayed(payload, request_id):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(payload, request_id)

        self.provider.create_promotion = delayed
        second_body = dict(self.body, task_id='another-task')
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.links.resolve, self.body)
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(PromotionError):
                    self.links.resolve(second_body)
                with self.links.connect() as conn:
                    pending = [json.loads(row[0]) for row in conn.execute('SELECT record_json FROM links')]
                frozen = next(row['short_url'] for row in pending if row['task_id']=='another-task')
            finally:
                release.set()
            first_result = first.result(3)
        second_result = self.links.resolve(second_body)
        self.assertEqual(second_result['short_url'], frozen)
        self.assertEqual(second_result['promotion_key'], first_result['promotion_key'])
        self.assertEqual(len(self.provider.calls), 1)

    def test_missing_creator_fails_before_network(self):
        with self.assertRaises(PromotionError): self.links.resolve(dict(self.body,actor={}))
        self.assertEqual(self.provider.calls, [])

    def test_delisted_drama_keeps_existing_link_but_no_new_context(self):
        first = self.links.resolve(self.body)
        self.db.drama['is_show'] = 0
        self.assertEqual(self.links.resolve(self.body), first)
        with self.assertRaises(PromotionError):
            self.links.resolve(dict(self.body,task_id='new-after-delisting'))
        self.assertEqual(len(self.provider.calls), 1)

    def test_targets_and_macro(self):
        self.assertTrue(safe_target('https://eng.moboreels.com/one'))
        for value in ['https://moboreels.com.attacker.test/a','https://user@eng.moboreels.com/a','http://eng.moboreels.com/a','https://eng.moboreels.com:123/a','https://eng.moboreels.com/a\nX:bad']:
            self.assertFalse(safe_target(value))
        self.assertTrue(contains_macro('before {{m_url}} after'))
        self.assertFalse(contains_macro('{m_url}', '{{url}}', None))


if __name__ == '__main__': unittest.main()
