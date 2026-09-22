from contextlib import contextmanager
import json
import os
import multiprocessing
from pathlib import Path
import tempfile
import sqlite3
import time
import unittest
from unittest.mock import patch

import tt_minis_storage as s


@contextmanager
def no_lock():
    yield


def concurrent_snapshot_worker(root, queue):
    s.STORAGE = Path(root)
    source = s.STORAGE / 'source.sqlite3'
    s.prepare_storage = lambda *args, **kwargs: source
    s.REFERENCE_ROOTS = []
    try:
        queue.put(('ok', str(s.snapshot('concurrent-test'))))
    except Exception as exc:
        queue.put(('error', repr(exc)))


class SnapshotLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'analysis-snapshots').mkdir()
        self.source = self.root / 'source.sqlite3'
        self.source.write_bytes(b'fixture database')
        for name, value in [('STORAGE', self.root), ('lifecycle_lock', no_lock),
                            ('prepare_storage', lambda *a, **k: self.source),
                            ('REFERENCE_ROOTS', []), ('open_snapshot_inodes', lambda: set())]:
            p = patch.object(s, name, value)
            p.start()
            self.addCleanup(p.stop)

    def make(self, index, age=10 * 86400, managed=True, lease=0, pin=None):
        path = self.root / 'analysis-snapshots' / ('tt_minis_cache_test_%d.sqlite3' % index)
        path.write_bytes(b'snapshot' * 100)
        now = time.time()
        os.utime(str(path), (now - age, now - age))
        if managed:
            registry = s.load_registry()
            registry['snapshots'][path.name] = {'created_at': now - age, 'last_used_at': now - age,
                'identity': s.file_identity(path), 'source_revision': s.source_revision(self.source),
                'lease_until': lease, 'pin': pin, 'owners': []}
            s.save_registry(registry)
        return path

    def test_snapshot_reuses_unchanged_source_and_renews_lease(self):
        path = self.make(1)
        with patch.object(s, 'create_snapshot') as create:
            self.assertEqual(path, s.snapshot('test-owner'))
        create.assert_not_called()
        rec = s.load_registry()['snapshots'][path.name]
        self.assertGreater(rec['lease_until'], time.time() + 86300)
        self.assertIn('test-owner', rec['owners'])

    def test_source_change_creates_new_snapshot(self):
        self.make(1)
        self.source.write_bytes(b'changed source')
        new = self.make(2, age=0, managed=False)
        with patch.object(s, 'create_snapshot', return_value=new) as create:
            self.assertEqual(new, s.snapshot())
        create.assert_called_once()
        self.assertIn(new.name, s.load_registry()['snapshots'])

    def test_changing_source_during_backup_disables_reuse(self):
        new = self.make(1, age=0, managed=False)
        def backup(_):
            self.source.write_bytes(b'new transaction')
            return new
        with patch.object(s, 'create_snapshot', side_effect=backup):
            s.snapshot()
        self.assertIsNone(s.load_registry()['snapshots'][new.name]['source_revision'])

    def test_latest_two_and_unmanaged_legacy_are_never_auto_deleted(self):
        legacy = self.make(0, managed=False)
        old = self.make(1)
        two = self.make(2, age=2)
        three = self.make(3, age=1)
        result = s.cleanup_managed(s.load_registry(), apply=True)
        self.assertEqual([old.name], [r['name'] for r in result['removed']])
        self.assertTrue(all(p.exists() for p in [legacy, two, three]))

    def test_lease_pin_reference_and_open_fd_protect_old_snapshots(self):
        lease = self.make(1, lease=time.time() + 100)
        pinned = self.make(2, pin='audit')
        referenced = self.make(3)
        opened = self.make(4)
        self.make(5, age=2)
        self.make(6, age=1)
        st = opened.stat()
        with patch.object(s, 'referenced_snapshots', return_value={referenced.name}), patch.object(
                s, 'open_snapshot_inodes', return_value={(st.st_dev, st.st_ino)}):
            result = s.cleanup_managed(s.load_registry(), apply=True)
        self.assertFalse(result['removed'])
        self.assertTrue(all(p.exists() for p in [lease, pinned, referenced, opened]))

    def test_changed_file_identity_blocks_cleanup_and_reuse(self):
        old = self.make(1)
        self.make(2, age=2)
        self.make(3, age=1)
        old_time = old.stat().st_mtime
        old.write_bytes(b'other contents')
        os.utime(str(old), (old_time, old_time))
        result = s.cleanup_managed(s.load_registry(), apply=True)
        self.assertFalse(result['removed'])
        self.assertEqual('identity changed', result['protected'][old.name])

    def test_dry_run_does_not_delete(self):
        old = self.make(1)
        self.make(2, age=2)
        self.make(3, age=1)
        result = s.cleanup_managed(s.load_registry())
        self.assertEqual(1, len(result['removed']))
        self.assertTrue(old.exists())

    def test_idle_third_snapshot_removed_even_when_recent_and_under_budget(self):
        old = self.make(1, age=100)
        self.make(2, age=2)
        self.make(3, age=1)
        result = s.cleanup_managed(s.load_registry(), apply=True)
        self.assertEqual([old.name], [r['name'] for r in result['removed']])
        self.assertFalse(old.exists())

    def test_newly_opened_snapshot_is_not_removed(self):
        old = self.make(1, age=100)
        self.make(2, age=2)
        self.make(3, age=1)
        st = old.stat()
        with patch.object(s, 'open_snapshot_inodes', side_effect=[set(), {(st.st_dev, st.st_ino)}]):
            result = s.cleanup_managed(s.load_registry(), apply=True)
        self.assertFalse(result['removed'])
        self.assertTrue(old.exists())

    def test_budget_refuses_to_evict_protected_snapshots(self):
        self.make(1, age=2)
        self.make(2, age=1)
        self.source.write_bytes(b'new source revision')
        with patch.object(s, 'MANAGED_BUDGET_BYTES', 1), patch.object(s, 'create_snapshot') as create:
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                s.snapshot()
            create.assert_not_called()

    def test_budget_evicts_only_unleased_oldest_managed_files(self):
        old = self.make(1, age=100)
        self.make(2, age=2)
        self.make(3, age=1)
        with patch.object(s, 'MANAGED_BUDGET_BYTES', 1600):
            result = s.cleanup_managed(s.load_registry(), apply=True)
        self.assertEqual([old.name], [r['name'] for r in result['removed']])

    def test_recursive_source_reference_scan(self):
        code = self.root / 'scripts' / 'nested'
        code.mkdir(parents=True)
        name = 'tt_minis_cache_20260911_095210_e3278e55.sqlite3'
        (code / 'analysis.py').write_text('path = "' + name + '"')
        with patch.object(s, 'REFERENCE_ROOTS', [self.root / 'scripts']):
            self.assertEqual({name}, s.referenced_snapshots())

    def test_extensionless_binary_is_not_treated_as_source(self):
        tool = self.root / 'ffmpeg'
        with tool.open('wb') as stream:
            stream.write(b'\x7fELF')
            stream.truncate(5 * 1024**2)
        with patch.object(s, 'REFERENCE_ROOTS', [self.root]):
            self.assertEqual(set(), s.referenced_snapshots())

    def test_oversized_text_reference_scan_still_fails_closed(self):
        text = self.root / 'huge.py'
        with text.open('wb') as stream:
            stream.truncate(5 * 1024**2)
        with patch.object(s, 'REFERENCE_ROOTS', [self.root]):
            with self.assertRaisesRegex(RuntimeError, 'scan limit'):
                s.referenced_snapshots()

    def test_candidate_identity_revalidated_and_protected_files_retained(self):
        old = self.make(1, managed=False)
        self.make(2, age=2)
        self.make(3, age=1)
        st = old.stat()
        item = {'name': old.name, 'path': str(old), 'inode': st.st_ino,
                'bytes': st.st_size, 'mtime': int(st.st_mtime), 'allocated': st.st_size}
        plan = {'candidates': [item]}
        with patch.object(s, 'referenced_snapshots', return_value={old.name}):
            self.assertEqual(1, len(s.retire_candidates(plan, True)['blocked']))
        item['inode'] += 1
        self.assertEqual(1, len(s.retire_candidates(plan, True)['blocked']))
        item['inode'] -= 1
        self.assertEqual(st.st_size, s.retire_candidates(plan, True)['allocated_bytes'])
        self.assertFalse(old.exists())

    def test_path_escape_and_corrupt_registry_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            s.snapshot_path('../tt_minis_cache_escape.sqlite3')
        (self.root / 'snapshot-registry.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'Invalid'):
            s.load_registry()

    @unittest.skipUnless(os.name == 'posix', 'Linux /proc check')
    def test_real_fd_scanner_finds_open_database(self):
        # Undo the fixture mock just for the actual Linux integration check.
        import importlib.util
        spec = importlib.util.spec_from_file_location('real_storage', s.__file__)
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        with self.source.open('rb') as stream:
            st = os.fstat(stream.fileno())
            self.assertIn((st.st_dev, st.st_ino), real.open_snapshot_inodes())

    @unittest.skipUnless(os.name == 'posix', 'Linux flock / SQLite CLI integration')
    def test_two_concurrent_requests_share_one_actual_sqlite_backup(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('real_storage', s.__file__)
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        self.source.unlink()
        with sqlite3.connect(str(self.source)) as conn:
            conn.execute('CREATE TABLE fixture (value TEXT)')
            conn.execute("INSERT INTO fixture VALUES ('preserved')")
        ctx = multiprocessing.get_context('fork')
        queue = ctx.Queue()
        with patch.object(s, 'lifecycle_lock', real.lifecycle_lock):
            # real.lifecycle_lock resolves globals on the newly loaded module.
            real.STORAGE = self.root
            processes = [ctx.Process(target=concurrent_snapshot_worker, args=(str(self.root), queue)) for _ in range(2)]
            for process in processes:
                process.start()
            results = [queue.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(30)
                self.assertEqual(0, process.exitcode)
        self.assertTrue(all(status == 'ok' for status, _ in results), results)
        self.assertEqual(results[0][1], results[1][1])
        with sqlite3.connect(results[0][1]) as conn:
            self.assertEqual('preserved', conn.execute('SELECT value FROM fixture').fetchone()[0])
        self.assertEqual(1, len(list((self.root / 'analysis-snapshots').glob('*.sqlite3'))))


if __name__ == '__main__':
    unittest.main()
