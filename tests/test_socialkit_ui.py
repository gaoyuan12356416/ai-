"""Browser acceptance tests against the actual report UI and snapshot query API."""
import functools
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from tests.test_socialkit_query import QueryTests
from features.socialkit_analytics import routes

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, 'Playwright is optional for browser acceptance')
class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture = QueryTests('test_separate_facts_do_not_multiply_revenue')
        self.fixture.setUp()
        root = self.fixture.root
        static = Path(__file__).resolve().parents[1] / 'static'
        class Handler(SimpleHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def _socialkit_analytics_actor(self):
                return {'role':'admin'}
            def do_GET(self):
                parsed=urlparse(self.path)
                if parsed.path=='/quick-nav.js':
                    payload=b'window.QuickNav={render:async()=>{}};'
                    self.send_response(200);self.send_header('Content-Type','text/javascript');self.end_headers();self.wfile.write(payload);return
                if parsed.path=='/api/ui/topbar':
                    return send_json(self,200,{'authenticated':True,'user':{'name':'Report Test','role':'admin'}})
                if parsed.path.startswith('/api/socialkit-analytics/'):
                    return routes.dispatch(self,parsed,{'SOCIALKIT_ANALYTICS_ROOT':str(root),'json_response':send_json})
                return super().do_GET()
        def send_json(handler,status,payload,**kwargs):
            data=json.dumps(payload,ensure_ascii=False).encode('utf-8')
            handler.send_response(status);handler.send_header('Content-Type','application/json');handler.send_header('Content-Length',str(len(data)));handler.end_headers();handler.wfile.write(data)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(static)))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.playwright=sync_playwright().start()
        try:
            self.browser=self.playwright.chromium.launch(headless=True)
        except Exception:
            self.playwright.stop();self.server.shutdown();self.server.server_close();self.fixture.tearDown()
            raise
        self.page=self.browser.new_page(viewport={'width':1440,'height':1000})
        self.errors=[];self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.goto('http://127.0.0.1:%s/socialkit-analytics.html'%self.server.server_port)
        self.page.locator('#report-content:not(.hidden)').wait_for()
        self.page.wait_for_function("document.querySelector('#export-report').disabled===false")

    def tearDown(self):
        self.browser.close();self.playwright.stop();self.server.shutdown();self.server.server_close();self.fixture.tearDown()

    def test_tabs_filters_charts_and_export_pin(self):
        self.assertIn('$18.99',self.page.locator('#kpi-grid').inner_text())
        self.assertEqual(self.page.locator('#trend-chart svg').count(),1)
        quality=self.page.locator('#quality-message').inner_text()
        self.assertIn('1 个未归属短链键',quality)
        self.assertIn('安装 1',quality)
        self.assertIn('收入 $0.00',quality)
        self.page.locator('#tab-effects').click()
        self.page.wait_for_function("document.querySelector('#report-period').textContent.includes('收入效果') && !document.querySelector('#export-report').disabled")
        self.assertEqual(self.page.locator('input[data-filter-search="country"]').count(),1)
        self.page.locator('details').filter(has=self.page.locator('[data-summary="country"]')).locator('summary').click()
        self.page.locator('input[data-filter="country"][value="US"]').check()
        self.assertTrue(self.page.locator('#export-report').is_disabled())
        self.page.locator('#apply-filters').click()
        self.page.wait_for_function("!document.querySelector('#export-report').disabled")
        self.fixture.make_snapshot('v2',2500)
        with self.page.expect_download() as downloaded:
            self.page.locator('#export-report').click()
        csv=Path(downloaded.value.path()).read_text(encoding='utf-8-sig')
        self.assertIn('v1',csv);self.assertIn('18.99',csv);self.assertNotIn('25.0',csv)
        self.page.locator('#refreshPage').click()
        self.page.wait_for_function("document.querySelector('#report-version').textContent.includes('v2')")
        self.assertIn('$25.00',self.page.locator('#kpi-grid').inner_text())
        self.page.locator('#tab-publications').click()
        self.page.wait_for_function("document.querySelector('#report-period').textContent.includes('发布执行') && !document.querySelector('#export-report').disabled")
        self.assertEqual(self.page.locator('[data-filter-search="country"]').count(),0)
        self.assertEqual(self.page.locator('[data-filter-search="reason"]').count(),1)
        self.assertIn('计划完成率',self.page.locator('#kpi-grid').inner_text())
        self.assertNotIn('$',self.page.locator('#kpi-grid').inner_text())
        self.assertEqual(self.errors,[])

    def test_failure_banner_and_mobile_layout(self):
        (self.fixture.root/'status.json').write_text(json.dumps({'status':'failed','error':'timeout'}))
        self.page.locator('#refreshPage').click()
        self.page.wait_for_function("!document.querySelector('#export-report').disabled")
        self.page.locator('#quality-message:not([hidden])').wait_for()
        self.assertIn('上一份成功快照',self.page.locator('#quality-message').inner_text())
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.wait_for_timeout(150)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),400)
        folder=Path(__file__).resolve().parents[1]/'output'/'playwright';folder.mkdir(parents=True,exist_ok=True)
        self.page.screenshot(path=str(folder/'socialkit-mobile.png'),full_page=True)
        self.page.set_viewport_size({'width':1440,'height':1000})
        self.page.screenshot(path=str(folder/'socialkit-desktop.png'),full_page=True)
        self.assertEqual(self.errors,[])

    def test_drama_language_group_filter_and_export(self):
        from features.socialkit_analytics.drama_language import enrich_snapshot
        from tests.test_socialkit_drama_language import Reader
        enrich_snapshot(self.fixture.root, Reader([dict(app_id=1479, content_id='abc', language='ja')]))
        self.page.locator('#refreshPage').click()
        self.page.wait_for_function("document.querySelector('#report-version').textContent.includes('-lang-')")
        self.page.locator('#tab-effects').click()
        self.page.wait_for_function("document.querySelector('#report-period').textContent.includes('收入效果') && !document.querySelector('#export-report').disabled")
        self.page.locator('#group-choices input[value="platform"]').uncheck()
        self.page.locator('#group-choices input[value="drama_language"]').check()
        self.page.locator('#apply-filters').click()
        self.page.wait_for_function("!document.querySelector('#export-report').disabled")
        self.assertIn('剧语言', self.page.locator('#report-head').inner_text())
        self.assertIn('日语', self.page.locator('#report-body').inner_text())
        self.assertIn('未提供', self.page.locator('#report-body').inner_text())
        self.page.locator('details').filter(has=self.page.locator('[data-summary="drama_language"]')).locator('summary').click()
        self.page.locator('input[data-filter="drama_language"][value="ja"]').check()
        self.page.locator('#apply-filters').click()
        self.page.wait_for_function("!document.querySelector('#export-report').disabled")
        self.assertIn('$18.99', self.page.locator('#kpi-grid').inner_text())
        self.assertNotIn('未提供', self.page.locator('#report-body').inner_text())
        with self.page.expect_download() as downloaded:
            self.page.locator('#export-report').click()
        csv = Path(downloaded.value.path()).read_text(encoding='utf-8-sig')
        self.assertIn('剧语言', csv); self.assertIn('日语', csv)
        self.assertEqual(self.errors, [])

    def test_utc_today_shortcut_warning_and_actual_yesterday(self):
        today = datetime.now(timezone.utc).date()
        self.assertIn('每小时整点', self.page.locator('#filter-hint').inner_text())
        self.page.locator('[data-range="today"]').click()
        self.assertEqual(self.page.locator('#date-start').input_value(), today.isoformat())
        self.assertEqual(self.page.locator('#date-end').input_value(), today.isoformat())
        self.page.locator('#apply-filters').click()
        self.page.wait_for_function("!document.querySelector('#export-report').disabled")
        self.assertIn('当前 UTC 日期的数据尚未完整', self.page.locator('#quality-message').inner_text())
        self.page.locator('[data-range="yesterday"]').click()
        self.assertEqual(self.page.locator('#date-start').input_value(), (today-timedelta(days=1)).isoformat())
        self.assertEqual(self.page.locator('#date-end').input_value(), (today-timedelta(days=1)).isoformat())
        self.assertEqual(self.errors, [])


if __name__=='__main__':
    unittest.main()
