import contextlib
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.gpu_offload.campaign import campaign_mapping as mapping
from features.gpu_offload.campaign.descriptions import open_descriptions


class Cursor:
    def __init__(self, membership, fail_on=None):
        self.membership = membership
        self.fail_on = fail_on
        self.details = []
        self.members = []

    def execute(self, sql, ids):
        detail = 'campaign_name' in sql
        (self.details if detail else self.members).extend(ids)
        if detail and self.fail_on in ids:
            raise TimeoutError('fixture deadline')
        self.rows = []
        for cid in ids:
            for ad in self.membership.get(cid, []):
                row = {'campaign_id': cid, 'ad_id': ad}
                if detail:
                    row.update(campaign_name='name-' + cid, product='A', country='US', language='en')
                self.rows.append(row)

    def fetchall(self):
        return self.rows


class ResumeTests(unittest.TestCase):
    def test_interruption_reuses_only_valid_descriptions_and_reads_all_memberships(self):
        now = dt.datetime(2026, 11, 24, 9)
        membership = {'1': ['10'], '2': ['20'], '3': ['30']}
        logs = [{'platform': 0, 'new_id': cid, 'app_name': 'A'} for cid in membership]
        source = ((0, 'db.source', 'campaign_id', 'product'),)
        with tempfile.TemporaryDirectory() as tmp, patch.object(mapping, 'DETAIL_BATCH', 1), \
                patch.object(mapping, 'SOURCE_SPECS', source), contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp) / 'labels.sqlite3'
            with open_descriptions(path, 'original-seed') as progress:
                with self.assertRaises(TimeoutError):
                    mapping.query_campaign_mapping(Cursor(membership, '3'), logs, None, now, progress)
            membership['2'] = ['99']
            next_read = Cursor(membership)
            with open_descriptions(path, 'original-seed') as progress:
                fresh, labels, _, _ = mapping.query_campaign_mapping(next_read, logs, None, now+dt.timedelta(minutes=15), progress)
            self.assertEqual(next_read.members, ['1','2','3'])
            self.assertEqual(next_read.details, ['2','3'])
            self.assertEqual({r['ad_id'] for r in fresh}, {'10','99','30'})
            self.assertEqual(labels[0,'1']['metadata_checked_at'], str(now))
            self.assertEqual(labels[0,'2']['metadata_checked_at'], str(now+dt.timedelta(minutes=15)))
            for seed, at in [('original-seed', now+dt.timedelta(days=2)), ('new-seed', now+dt.timedelta(days=2,minutes=1))]:
                next_read = Cursor(membership)
                with open_descriptions(path, seed) as progress:
                    mapping.query_campaign_mapping(next_read, logs, None, at, progress)
                self.assertEqual(next_read.details, ['1','2','3'])

    def test_a_different_source_or_future_check_cannot_supply_labels(self):
        now = dt.datetime(2026, 11, 24, 9)
        labels = {(0,'1'):dict(metadata_signature='match', metadata_checked_at=str(now),
                                campaign_name='A', product='P', countries='US', languages='en')}
        with tempfile.TemporaryDirectory() as tmp:
            with open_descriptions(Path(tmp)/'labels.sqlite3','seed') as progress:
                progress.save('primary',0,['1'],labels)
                for source, at in [('fallback',now),('primary',now-dt.timedelta(seconds=1))]:
                    self.assertEqual(progress.restore(source,0,['1'],labels,at,dt.timedelta(days=1)), ['1'])
