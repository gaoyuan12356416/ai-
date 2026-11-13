import gzip
import hashlib
import importlib.util
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('private_archive', Path(__file__).parents[1] / 'scripts/private_storage_archive.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeCOS:
    def __init__(self):
        self.parts = []
        self.aborted = False
        self.complete = False

    def create_multipart_upload(self, **kw):
        self.metadata = kw['Metadata']
        assert kw['ACL'] == 'private'
        return {'UploadId': 'upload'}

    def upload_part(self, **kw):
        self.parts.append(kw['Body'])
        return {'ETag': str(len(self.parts))}

    def complete_multipart_upload(self, **kw):
        self.complete = True

    def abort_multipart_upload(self, **kw):
        self.aborted = True

    def get_object(self, **kw):
        raw = io.BytesIO(b''.join(self.parts))
        return {'Body': types.SimpleNamespace(get_raw_stream=lambda: raw)}


class ArchiveTest(unittest.TestCase):
    def archive(self):
        a = m.Archive.__new__(m.Archive)
        a.client, a.bucket, a.prefix = FakeCOS(), 'private', 'storage-archive/v1'
        a.privacy = lambda key: None
        return a

    def test_streaming_gzip_roundtrip_and_corruption(self):
        data = b'known immutable database rows\n' * 10000
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'db.sqlite3'
            path.write_bytes(data)
            a = self.archive()
            h = hashlib.sha256(data).hexdigest()
            a.upload(path, 'storage-archive/v1/object', h, m.signature(path), 'gzip')
            self.assertEqual(gzip.decompress(b''.join(a.client.parts)), data)
            restored = io.BytesIO()
            a.verify('storage-archive/v1/object', h, len(data), 'gzip', restored)
            self.assertEqual(restored.getvalue(), data)
            with self.assertRaises(RuntimeError):
                a.verify('storage-archive/v1/object', '0' * 64, len(data), 'gzip')

    def test_changed_source_aborts_before_object_commit(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'db.sqlite3'
            p.write_bytes(b'old')
            sig = m.signature(p)
            p.write_bytes(b'changed')
            a = self.archive()
            with self.assertRaises(RuntimeError):
                a.upload(p, 'key', hashlib.sha256(b'old').hexdigest(), sig, 'gzip')
            self.assertTrue(a.client.aborted)
            self.assertFalse(a.client.complete)

    def test_public_acl_rejected(self):
        with self.assertRaises(RuntimeError):
            m.Archive._private_acl({'AccessControlList': {'Grant': [{'Grantee': {'URI': 'AllUsers'}}]}})

    def test_escape_key_rejected(self):
        for key in ['another/object', 'storage-archive/v1/../secret']:
            with self.assertRaises(ValueError):
                m.safe_key(key, 'storage-archive/v1')

    def test_failed_archive_preserves_source(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'source.db'
            p.write_bytes(b'database')
            plan = Path(td) / 'plan.json'
            plan.write_text(json.dumps({'namespace': 'test', 'allowed_roots': [td],
                                        'files': [{'path': str(p), 'signature': m.signature(p)}]}))
            args = types.SimpleNamespace(plan=str(plan), manifest=str(Path(td) / 'journal.jsonl'), delete_verified=True)
            a = types.SimpleNamespace(put=lambda *args: (_ for _ in ()).throw(RuntimeError('failed')))
            self.assertEqual(m.batch(a, args), 1)
            self.assertTrue(p.exists())


if __name__ == '__main__':
    unittest.main()
