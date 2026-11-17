"""A continued collection reuses its validated snapshot; new stale reads fail."""
from datetime import datetime,timezone,date,timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.fb_page_portrait.collect import check_social,social_input
from features.fb_page_portrait.partition_cache import PartitionCache


class SocialResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.warehouse=self.root/'warehouse';self.warehouse.mkdir()
        self.meta=dict(version='immutable',generated_at='2026-11-17T02:15:00+00:00',catalog_errors={},
            coverage={'facebook:'+site:[str(date(2026,11,10)+timedelta(days=i)) for i in range(7)] for site in ('2049','2310')})
        (self.warehouse/'current.json').write_text(json.dumps(self.meta))
        self.instant=datetime(2026,11,17,5,0,tzinfo=timezone.utc)
        owner=self
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return owner.instant
        self.clock=patch('features.fb_page_portrait.collect.datetime',Clock);self.clock.start();self.addCleanup(self.clock.stop)
    def cache(self):return PartitionCache(['1'],'2026-11-10','2026-11-16',self.root/'collect-cache')
    def load(self,cache):return social_input(cache,['1'],'2026-11-10','2026-11-16',self.warehouse)

    def test_new_collection_rejects_current_stale_source(self):
        self.instant=datetime(2026,11,17,6,0,tzinfo=timezone.utc)
        with self.assertRaisesRegex(RuntimeError,'social_snapshot_stale'):self.load(self.cache())

    def test_continuation_uses_identical_previously_validated_source(self):
        cache=self.cache()
        with patch('features.fb_page_portrait.collect.social',return_value=([{'page_id':'1','revenue_cents':5}],'immutable')) as read:
            before=self.load(cache)
            self.instant=datetime(2026,11,17,6,0,tzinfo=timezone.utc)
            # Even a later broken pointer cannot alter already accepted inputs.
            (self.warehouse/'current.json').write_text('{}')
            after=self.load(cache)
            self.assertEqual(before,after);self.assertEqual(read.call_count,1)
            self.assertEqual(after['validated_at'],'2026-11-17T05:00:00+00:00')

    def test_corrupt_rows_are_not_silently_revalidated(self):
        cache=self.cache()
        with patch('features.fb_page_portrait.collect.social',return_value=([],'immutable')):self.load(cache)
        path=cache.folder/'social-input.json';saved=json.loads(path.read_text());saved['rows']=[{'bad':True}];path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(RuntimeError,'frozen_social_input_changed'):self.load(cache)

    def test_missing_coverage_is_still_a_source_failure(self):
        self.meta['coverage']['facebook:2310']=[]
        with self.assertRaisesRegex(RuntimeError,'social_days_incomplete'):check_social(self.meta,'2026-11-10','2026-11-16',self.instant)


if __name__=='__main__':unittest.main()
