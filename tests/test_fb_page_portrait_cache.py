"""Failed historical reads retain completed slices, never partial/empty output."""
import json
from pathlib import Path
import tempfile
import unittest

from features.fb_page_portrait.partition_cache import PartitionCache,complete


class Reader:
    def __init__(self):self.calls=0;self.error=None
    def query(self,statements,timeout=45):
        self.calls+=1
        if self.error:raise RuntimeError(self.error)
        return [{'value':self.calls}]


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'collect-cache';self.reader=Reader()
    def cache(self,scope=None):return PartitionCache(scope or ['1'],'2026-11-10','2026-11-16',self.root)

    def test_successful_partition_resumes_same_collection(self):
        cache=self.cache();result=cache.query(self.reader,['SELECT historical'])
        later=self.cache();self.assertEqual(later.folder,cache.folder)
        self.assertEqual(later.query(self.reader,['SELECT historical']),result)
        self.assertEqual(self.reader.calls,1)

    def test_failed_slice_not_cached_or_converted_to_zero(self):
        cache=self.cache();self.reader.error='source_query_failed_3024'
        with self.assertRaises(RuntimeError):cache.query(self.reader,['SELECT historical'])
        self.reader.error=None
        self.assertEqual(cache.query(self.reader,['SELECT historical']),[{'value':2}])
        self.assertEqual(self.reader.calls,2)

    def test_retry_is_limited_to_one_safe_read(self):
        self.reader.error='source_query_failed_rc_124'
        with self.assertRaises(RuntimeError):self.cache().query(self.reader,['SELECT historical'])
        self.assertEqual(self.reader.calls,2)

    def test_scope_and_sql_changes_cannot_reuse_other_data(self):
        cache=self.cache();cache.query(self.reader,['SELECT one'])
        self.assertNotEqual(self.cache(['2']).folder,cache.folder)
        cache.query(self.reader,['SELECT two']);self.assertEqual(self.reader.calls,2)

    def test_complete_cycle_refreshes_and_prunes_only_complete_caches(self):
        incomplete=self.cache(['preserved'])
        for _ in range(4):
            cache=self.cache();cache.query(self.reader,['SELECT historical'])
            complete({'collection':cache.metadata()})
        completed=[folder for folder in self.root.iterdir() if folder.is_dir()
            and json.loads((folder/'manifest.json').read_text()).get('complete')]
        self.assertEqual(len(completed),2);self.assertTrue(incomplete.folder.exists())
        self.assertNotEqual(self.cache().folder,cache.folder)


if __name__=='__main__':unittest.main()
