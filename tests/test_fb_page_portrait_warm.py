"""A concurrent readonly warmer never replaces an already completed input."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys


class WarmTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[1]/'ops/meta-page-recovery/warm_catalog.py'
        spec=importlib.util.spec_from_file_location('warm_catalog',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.cache=self.module.WarmCache(Path(self.temp.name))
    def test_completed_result_avoids_repeated_source_read(self):
        class Reader:
            def __init__(self):self.calls=0
            def query(self,*args,**kw):self.calls+=1;return [{'value':1}]
        reader=Reader()
        self.assertEqual(self.cache.query(reader,['SELECT bounded']),[{'value':1}])
        self.assertEqual(self.cache.query(reader,['SELECT bounded']),[{'value':1}]);self.assertEqual(reader.calls,1)
    def test_direct_cli_imports_its_own_checkout(self):
        path=Path(__file__).resolve().parents[1]/'ops/meta-page-recovery/warm_catalog.py'
        result=subprocess.run([sys.executable,str(path),'--help'],cwd=self.temp.name,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_active_collectors_result_wins_a_concurrent_finish(self):
        cache=self.cache;module=self.module
        class Reader:
            def query(self,statements,**kw):
                path=cache.folder/(module.hashlib.sha256(module.encoded(statements).encode()).hexdigest()+'.json')
                path.write_text(json.dumps({'rows':[{'value':'collector'}]}))
                return [{'value':'warmer'}]
        self.assertEqual(cache.query(Reader(),['SELECT bounded']),[{'value':'collector'}])
        self.assertFalse(list(cache.folder.glob('warm-*')))


if __name__=='__main__':unittest.main()
