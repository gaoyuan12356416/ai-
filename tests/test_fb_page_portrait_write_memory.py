"""Keep one page's JSON live while retaining exact original content/hash keys."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref
import subprocess
import sys

from features.fb_page_portrait import storage
from features.fb_page_portrait.resume import restore


class MemoryWriteTests(unittest.TestCase):
    def test_memory_canary_direct_cli_has_no_driver_requirement_for_help(self):
        path=Path(__file__).resolve().parents[1]/'ops/meta-page-recovery/profile_memory_canary.py'
        with tempfile.TemporaryDirectory() as folder:
            result=subprocess.run([sys.executable,str(path),'--help'],cwd=folder,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_writer_does_not_retain_all_encoded_page_payloads(self):
        alive=weakref.WeakSet();peak=[0];original=storage.encoded;written={};batches=[0]
        class Tracked(str):pass
        def encode(value):
            result=original(value)
            if isinstance(value,dict) and 'page_id' in value:
                result=Tracked(result);alive.add(result);peak[0]=max(peak[0],len(alive))
            return result
        class Cursor:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def fetchone(self):return ('ads_ai',0)
            def executemany(self,sql,values):
                if isinstance(values,(list,tuple)):raise AssertionError('all_page_payloads_materialized')
                batches[0]+=1
                for item in values:self.execute(sql,item)
            def execute(self,sql,values=None):
                if sql.startswith('INSERT'):
                    written[values[1]]=dict(page_id=values[1],hash=hashlib.sha256(values[8].encode()).hexdigest(),paid_cents=values[4],social_cents=values[5],spend_cents=values[6])
        class DB:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def cursor(self):return Cursor()
            def commit(self):pass
        class Reader:
            def query(self,sql,timeout=30):
                if "'rows',COUNT(*)" in sql[0]:return [{'rows':len(written)}]
                return list(written.values())
        pages={str(i):dict(page_id=str(i),paid_cents=1,social_cents=2,spend_cents=3,audiences=[],padding='x'*10000) for i in range(100)}
        data=dict(pages=pages,meta=dict(version='20261117T061854638718Z',start='2026-11-10',end='2026-11-16',
            audit=dict(paid_cents=100,social_cents=200,spend_cents=300)))
        with patch.object(storage,'encoded',side_effect=encode),patch.dict('sys.modules',{'pymysql':SimpleNamespace(connect=lambda **kw:DB())}):
            proof=storage.write(data,{'ADMIN_MAPPING_MYSQL_USER':'fixture','ADMIN_MAPPING_MYSQL_PASSWORD':'fixture'},Reader())
        self.assertEqual(proof['verified_rows'],100);self.assertLess(peak[0],10);self.assertEqual(batches[0],1)
        self.assertEqual(proof['payload_hashes'],{pid:hashlib.sha256(original(p).encode()).hexdigest() for pid,p in pages.items()})
    def test_hash_readback_splits_timeout_without_omitting_pages(self):
        class Reader:
            def query(self,statements,timeout=30):
                import re
                sql=statements[0]
                if "'rows',COUNT(*)" in sql:return [{'rows':5}]
                ids=[bytes.fromhex(value[2:]).decode() for value in re.search(r'page_id IN \(([^)]+)\)',sql).group(1).split(',')]
                if len(ids)>2:raise RuntimeError('source_query_failed_3024')
                return [{'page_id':pid,'hash':pid} for pid in ids]
        rows=storage.readback(Reader(),'20261117T061854638718Z',['1','2','3','4','5'])
        self.assertEqual({row['page_id'] for row in rows},{'1','2','3','4','5'});self.assertEqual(len(rows),5)


class ResumeTests(unittest.TestCase):
    def test_same_version_and_original_input_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'20261117T061854638718Z.input.json';path.write_text('{"frozen":true}')
            before=path.read_bytes()
            with patch('features.fb_page_portrait.resume.build',return_value={'meta':{'version':'other'},'pages':{}}):raw,data=restore(root,path)
            self.assertEqual(data['meta']['version'],'20261117T061854638718Z')
            self.assertEqual(data['meta']['generated_at'],'2026-11-17T06:18:54.638718+00:00')
            self.assertEqual(path.read_bytes(),before)
    def test_published_newer_business_version_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'20261117T061854638718Z.input.json';path.write_text('{}')
            (root/'current.json').write_text('{"version":"20261117T071854638718Z"}')
            with self.assertRaisesRegex(ValueError,'superseded'):restore(root,path)


if __name__=='__main__':unittest.main()
