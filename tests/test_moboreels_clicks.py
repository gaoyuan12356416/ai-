from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_moboreels_service import Handler
from test_moboreels_promotions import ACTOR, FakeDB, FakeProvider
from features.moboreels_library.clicks import BEIJING, ClickStats, traffic_bucket, traffic_source
from features.moboreels_library.promotions import PromotionManager
from features.moboreels_library.settings import Settings
from features.moboreels_library.short_links import ShortLinks
from scripts.moboreels_click_history import capture, import_history


class ClickTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.settings = Settings(self.dir, 'institution', 'test', 'test', 'test', 'test')
        db = FakeDB()
        self.links = ShortLinks(self.settings, db, PromotionManager(self.settings, db, FakeProvider()))
        self.stats = ClickStats(self.settings)
        self.admin = dict(ACTOR, is_admin=True)

    def link(self, **changes):
        body = dict(actor=ACTOR, story_id='42', platform='x', account_id='acct', task_id='task',
                    material_id='mat', purpose='post')
        body.update(changes)
        code = self.links.resolve(body)['short_url'].rsplit('/', 1)[1]
        return self.links.redirect_record(code)

    def test_real_http_and_fail_open(self):
        record = self.link()
        code = record['short_code']
        validation = self.link(purpose='validation')['short_code']
        server = HTTPServer(('127.0.0.1', 0), Handler)
        server.app = SimpleNamespace(short_links=self.links, clicks=self.stats)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        worker.start()
        def send(code, method='GET', **headers):
            conn = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                conn.request(method, '/m/'+code, headers=headers)
                reply = conn.getresponse()
                reply.read()
                return reply.status, reply.getheader('Location')
            finally:
                conn.close()
        try:
            self.assertEqual(send(code+'?platform=fb', Referer='https://t.co/post'),
                             (302, record['target_url']))
            self.assertEqual(send(code, **{'User-Agent':'Twitterbot/1.0'})[0], 302)
            self.assertEqual(send(code, **{'Sec-Purpose':'prefetch;prerender'})[0], 302)
            self.assertEqual(send(validation)[0], 302)
            self.assertEqual(send('invalid')[0], 404)
            self.assertNotEqual(send(code, 'HEAD')[0], 302)
            totals = self.stats.query({}, ACTOR)['summary']
            self.assertEqual(totals, {'clicks':1, 'links_clicked':1, 'automated':2})
            with patch.object(self.stats, 'connect', side_effect=sqlite3.OperationalError('unavailable')):
                with self.assertLogs(level='ERROR'):
                    self.assertEqual(send(code), (302, record['target_url']))
                    send('invalid')  # the preceding single-threaded handler has returned
        finally:
            server.shutdown();worker.join(2);server.server_close()

    def test_scope_grouping_and_concurrency(self):
        one = self.link()
        two = self.link(platform='fb', task_id='other')
        foreign = self.link(actor=dict(ACTOR, tenant_key='another', name='同名另一人'))
        for link in (one,two,foreign):
            self.stats.record_redirect(link, {'Referer':'https://t.co/forwarded'})
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.stats.record_redirect(one, {}), range(12)))
        args = {'all_users':'true', 'is_admin':'true', 'tenant_key':'another'}
        own = self.stats.query(args, ACTOR)
        self.assertEqual(own['summary'], {'clicks':14, 'automated':0, 'links_clicked':2})
        self.assertEqual({(r['platform'],r['publisher_name']) for r in own['list']},
                         {('fb', ACTOR['name']), ('x', ACTOR['name'])})
        self.assertEqual(self.stats.query({}, dict(ACTOR, user_id='absent'))['summary']['clicks'], 0)
        all_stats = self.stats.query({'group_by':'publisher'}, self.admin)
        self.assertEqual(all_stats['total'], 2)
        self.assertEqual(all_stats['summary']['clicks'], 15)
        for dimension in ('referrer','account','material','story','day','platform'):
            report = self.stats.query({'group_by':dimension, 'page_size':'1'}, self.admin)
            self.assertEqual(len(report['list']), 1)
            self.assertEqual(report['summary']['links_clicked'], 3)
        self.assertEqual(self.stats.query({'platform':'fb'}, self.admin)['summary']['clicks'], 1)
        for params in ({'group_by':'bogus'}, {'page_size':0}, {'platform':'bogus'},
                       {'date_from':'2026-01-01', 'date_to':'2026-02-01'}):
            with self.assertRaises(ValueError):
                self.stats.query(params, ACTOR)

    def test_sources_and_midnight(self):
        instant = datetime(2026, 11, 18, 16, tzinfo=timezone.utc)
        self.assertEqual(traffic_bucket(instant, 'https://l.facebook.com/r', 'Mozilla'),
                         ('2026-11-19','fb',1,0))
        self.assertEqual(traffic_bucket(instant-timedelta(seconds=1), '', 'Twitterbot/1.0'),
                         ('2026-11-18','unknown',0,1))
        self.assertEqual(traffic_source(None,'Twitter for iPhone/12.2'), 'x')
        self.assertEqual(traffic_source('https://facebook.com.example.org','Twitter for iPhone/12.2'), 'other')
        self.assertEqual(traffic_source('https://[invalid','Safari'), 'unknown')
        for ua in ['trendictionbot0.0', 'Googlebot-Image/1', 'facebookexternalhit/1', 'curl/8.7']:
            self.assertEqual(traffic_bucket(instant,'',ua)[3], 1)

    def test_bounded_history_import_once_and_live_same_day(self):
        link = self.link()
        code = link['short_code']
        test_code = self.link(purpose='validation')['short_code']
        day = datetime.now(BEIJING)
        stamp = day.strftime('%d/%b/%Y:%H:%M:%S %z')
        def row(code, agent='Mozilla', method='GET', status=302):
            return f'127.0.0.1 - - [{stamp}] "{method} /m/{code}?q=a HTTP/2.0" {status} 0 "-" "{agent}" "-"\n'.encode()
        log = self.dir/'access.log'
        log.write_bytes(row(code)+row(code,'Twitterbot/1')+row(code, method='HEAD')+
                        row(code,status=404)+row(test_code)+row('D'*16))
        prefix = capture(self.dir)
        with log.open('ab') as out:
            out.write(row(code))
        self.stats.record_redirect(link, {})
        first = import_history(self.stats, prefix)
        self.assertEqual((first['clicks'], first['automated']), (1,1))
        self.assertEqual(import_history(self.stats, prefix), first)
        self.assertEqual(self.stats.query({}, ACTOR)['summary'], {'clicks':2,'automated':1,'links_clicked':1})
        with self.assertRaises(ValueError):
            import_history(self.stats, capture(self.dir))


if __name__ == '__main__':
    unittest.main()
