"""Committed payload recovery preserves exact arrays, cents and snapshot keys."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from features.fb_page_portrait.recover_committed import financial_input,stored_items
from features.fb_page_portrait.storage import publish,Store
from features.fb_page_portrait.runtime import encoded


class CommittedTests(unittest.TestCase):
    def test_raw_income_check_does_not_reclassify(self):
        raw=dict(page_ids=['1','2'],paid=[dict(page_id='1',revenue='1.25',spend='2.5')],social=[dict(page_id='1',revenue_cents=3)])
        pages,audit=financial_input(raw)
        self.assertEqual(pages['1'],dict(paid_cents=125,social_cents=3,spend_cents=250))
        self.assertEqual(pages['2'],dict(paid_cents=0,social_cents=0,spend_cents=0));self.assertEqual(audit,pages['1'])
    def test_exact_stored_payload_is_projected_without_a_model_rebuild(self):
        page=dict(page_id='1',paid_cents=125,social_cents=3,spend_cents=250,audiences=[],custom_order=['b','a'])
        body=encoded(page);digest=hashlib.sha256(body.encode()).hexdigest()
        class Reader:
            def query(self,*args,**kw):return [dict(page_id='1',detail=body,hash=digest,stored_hash=digest,paid_cents=125,social_cents=3,spend_cents=250)]
        expected={'1':dict(paid_cents=125,social_cents=3,spend_cents=250)}
        rows=stored_items(Reader(),'20261117T061854638718Z',{'1':digest},expected)
        with tempfile.TemporaryDirectory() as temporary:
            meta=dict(version='20261117T061854638718Z',page_count=1,audit=expected['1'])
            proof=dict(version=meta['version'],verified_rows=1,payload_hashes={'1':digest})
            publish({'meta':meta},proof,temporary,page_items=rows)
            result=Store(temporary).load(['1'],detail=True)
            self.assertEqual(result['items']['1']['custom_order'],['b','a'])
    def test_hash_or_income_conflict_never_yields_a_published_page(self):
        page=dict(page_id='1',paid_cents=1,social_cents=0,spend_cents=0,audiences=[]);body=encoded(page);digest=hashlib.sha256(body.encode()).hexdigest()
        class Reader:
            def query(self,*args,**kw):return [dict(page_id='1',detail=body,hash=digest,stored_hash=digest,paid_cents=1,social_cents=0,spend_cents=0)]
        with self.assertRaisesRegex(RuntimeError,'income_mismatch'):
            list(stored_items(Reader(),'20261117T061854638718Z',{'1':digest},{'1':dict(paid_cents=2,social_cents=0,spend_cents=0)}))


if __name__=='__main__':unittest.main()
