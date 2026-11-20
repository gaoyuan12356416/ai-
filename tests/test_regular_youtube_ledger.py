"""A publish worker must see approved bytes in its very first claim."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from features.drama_synthesis.core import DramaSynthesisError, DramaSynthesisStore


class RegularYouTubeLedgerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.store = DramaSynthesisStore(root / 'tasks.sqlite')
        self.store.ensure_storage()
        self.request = dict(operation_id='youtube-auto-test-regular', preparation_id='c' * 32,
                            job_id='d' * 32, content_id='123', app_id='1479', channel_local_id='1',
                            channel_id='UC' + 'x' * 22, youtube_account_id='7', source_kind='custom_source',
                            source_url='https://example.org/movie.mp4', title='Drama', description_template='Watch',
                            description_rendered='Watch', comment_text='', duplicate_confirmed=False,
                            scopes=['youtube'], source_material_id='130', approved_cover_path=str(root/'cover.jpg'),
                            approved_cover_sha256='a' * 64)

    def test_concurrent_confirms_freeze_identical_bytes_in_first_insert(self):
        with closing(sqlite3.connect(self.store.db_path)) as db:
            db.execute("""CREATE TRIGGER capture_first_insert AFTER INSERT ON drama_youtube_publish
                WHEN NEW.source_sha256='' OR NEW.source_size=0 BEGIN SELECT RAISE(ABORT,'missing bytes'); END""")
        request = dict(self.request, expected_source_sha256='b' * 64, expected_source_size=123)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.enqueue_reviewed_youtube(**request), [0, 1]))
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual((results[0]['source_sha256'], results[0]['source_size']), ('b' * 64, 123))
        with self.assertRaises(DramaSynthesisError) as exc:
            self.store.enqueue_reviewed_youtube(**dict(request, expected_source_size=124))
        self.assertEqual(exc.exception.code, 'youtube_operation_conflict')

    def test_legacy_reviewed_caller_and_partial_fingerprint(self):
        with self.assertRaises(DramaSynthesisError) as exc:
            self.store.enqueue_reviewed_youtube(**self.request, expected_source_sha256='b' * 64)
        self.assertEqual(exc.exception.code, 'youtube_reviewed_source_invalid')
        value = self.store.enqueue_reviewed_youtube(**self.request)
        self.assertEqual((value['source_sha256'], value['source_size']), ('', 0))
