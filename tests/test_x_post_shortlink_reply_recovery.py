import importlib.util
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

PATH = Path(__file__).resolve().parents[1] / 'scripts/x_post_shortlink_reply_recovery.py'
SPEC = importlib.util.spec_from_file_location('reply_recovery', PATH)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class ReplyRecoveryTests(unittest.TestCase):
    def test_exact_render_and_macro_rejection(self):
        self.assertEqual(m.reply_text('https://gy.g2flow.com/s/171'), 'Watch Full Now 👉https://gy.g2flow.com/s/171')
        for value in ['{{url}}','https://gy.g2flow.com/s/{{url}}','https://evil.test/s/1',
                      'https://gy.g2flow.com/s/1\nhello','https://gy.g2flow.com/s/1?x=2']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.reply_text(value)

    def test_reply_readback_binds_author_parent_and_link(self):
        text = m.reply_text('https://gy.g2flow.com/s/171')
        post = {'author_id':'123','text':m.PREFIX+'https://t.co/abc',
                'referenced_tweets':[{'type':'replied_to','id':'456'}],
                'entities':{'urls':[{'url':'https://t.co/abc','expanded_url':'https://gy.g2flow.com/s/171'}]}}
        self.assertTrue(m.is_reply(post, '456', '123', text))
        self.assertFalse(m.is_reply(post, '457', '123', text))
        self.assertFalse(m.is_reply(post, '456', '999', text))
        self.assertFalse(m.is_reply(post, '456', '123', m.reply_text('https://gy.g2flow.com/s/172')))

    def test_missing_link_not_confused_by_text(self):
        self.assertFalse(m.has_link('🎬 Show\nDescription... #shortdrama'))
        self.assertTrue(m.has_link('Watch https://gy.g2flow.com/s2l-tag/123.html'))

    def test_redirect_requires_exact_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = 'https://example.test/?af_c_id=55&af_dp=drama'
            (root/'55.html').write_text('<script>location.replace('+json.dumps(target)+');</script>', encoding='utf8')
            row = {'lane':'tag','log_id':55,'short_url':'https://gy.g2flow.com/s2l-tag/55.html','long_url':target}
            m.validate_link(row, {'tag':directory}, '')
            row['long_url'] += 'wrong'
            with self.assertRaisesRegex(ValueError,'redirect_target_mismatch'):
                m.validate_link(row, {'tag':directory}, '')

    def test_shared_link_cross_lane_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            c = sqlite3.connect(root/'links.sqlite3')
            c.execute('CREATE TABLE short_link(id INTEGER, origin TEXT, log_id INTEGER, long_url TEXT)')
            c.execute("INSERT INTO short_link VALUES(1,'x',55,'https://target.test')")
            c.commit(); c.close()
            row = {'lane':'tag','log_id':55,'short_url':'https://gy.g2flow.com/s/1','long_url':'https://target.test'}
            with self.assertRaisesRegex(ValueError,'short_mapping_mismatch'):
                m.validate_link(row, {}, directory)

    def test_durable_unknown_is_not_ready_and_receipt_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'state.sqlite3')
            c = m.ledger(path)
            c.execute("INSERT INTO reply(post_id,account_id,username,short_url,text,source_json,updated_at) VALUES('123',1,'u','https://gy.g2flow.com/s/1','text','{}','now')")
            c.commit()
            m.mark(c, {'post_id':'123'}, 'unknown', error='timeout')
            c.close()
            c = m.ledger(path)
            self.assertEqual(c.execute("SELECT count(*) FROM reply WHERE status='ready'").fetchone()[0],0)
            m.mark(c, {'post_id':'123'}, 'published', reply_id='456', verified=1)
            c.close()
            with contextlib.closing(m.ro(path)) as c:
                self.assertEqual(tuple(c.execute('SELECT reply_id,verified FROM reply').fetchone()),('456',1))

    def test_network_write_timeout_is_unknown(self):
        class Failing:
            def open(self,*a,**kw):
                raise TimeoutError()
        with patch.object(m.urllib.request,'build_opener',return_value=Failing()):
            with self.assertRaises(m.ApiError) as result:
                m.api('never-logged','POST','/2/tweets',{'text':'x'})
        self.assertTrue(result.exception.uncertain)
        self.assertNotIn('never-logged',str(result.exception))


if __name__ == '__main__':
    unittest.main()
